#!/usr/bin/env bash
# ===========================================================================
# testhub.sh -- set up, start and stop the Test Hub on this machine.
#
# The Test Hub and its built-in demo website (Acme Freight) are ONE process:
# starting the hub starts the demo site, stopping it stops both.
#
#   ./testhub.sh setup              one time: Python packages + browsers
#                                   (+ the TypeScript engine when Node.js is here)
#   ./testhub.sh start              start in the background
#   ./testhub.sh start --foreground run in this terminal (Ctrl+C stops it)
#   ./testhub.sh stop [--force]     stop it (tests still running are stopped cleanly)
#   ./testhub.sh restart            stop + start
#   ./testhub.sh status             is it running, and where
#   ./testhub.sh logs [-f]          the server log (-f: follow it)
#
#   ./testhub.sh run DEMO-010 ...   queue tests (the running hub executes them)
#   ./testhub.sh demo-release 2.1.0 deploy a release of the demo site
#   ./testhub.sh typecheck          type-check your TypeScript tests
#   ./testhub.sh unit-tests         the hub's own unit test suite
#   ./testhub.sh manage <command>   any other hub command (doctor, backup,
#                                   samples, export_tests, ...)
#
# macOS or Linux (Windows: inside WSL2). Needs internet for `setup` only.
# ===========================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"   # physical path: compared with process cwds
APP="${REPO}/app"
VENV="${REPO}/.venv"
VPY="${VENV}/bin/python"
BROWSERS="${REPO}/.browsers"          # Chromium for the Python tests
ENGINE="${REPO}/.pw-ts"               # the TypeScript engine: node + @playwright/test + its Chromium
RUN_DIR="${REPO}/.run"
PIDFILE="${RUN_DIR}/testhub.pid"
LOG="${RUN_DIR}/testhub.log"

# Every hub process (the server, and the test runs it starts) finds the
# browsers and the TypeScript engine through these.
export PLAYWRIGHT_BROWSERS_PATH="$BROWSERS"
export PW_TS_ENGINE="$ENGINE"

if [[ -t 1 ]]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
    B=""; G=""; Y=""; R=""; N=""
fi
say()  { echo "${B}==>${N} $*"; }
ok()   { echo "  ${G}ok${N}  $*"; }
warn() { echo "  ${Y}!!${N}  $*"; }
die()  { echo "${R}error:${N} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
py_version() {                        # "3.12" for a python executable, or nothing
    "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

supported_python() {                  # prints the path of a Python 3.9-3.12
    local c p
    for c in ${PYTHON:-python3.12 python3.11 python3.10 python3.9 python3}; do
        p="$(command -v "$c" 2>/dev/null)" || continue
        case "$(py_version "$p")" in
            3.9|3.10|3.11|3.12) echo "$p"; return 0 ;;
        esac
    done
    return 1
}

why_not() {                           # the one line of a browser failure worth reading
    local line
    line="$(printf '%s\n' "$1" | grep -m 1 -o 'error while loading shared libraries: [^ ]*' || true)"
    [[ -n "$line" ]] || line="$(printf '%s\n' "$1" | grep -v '^\s*$' | tail -n 1)"
    line="${line%:}"
    printf '%s' "${line:0:200}"
}

need_setup() {
    [[ -x "$VPY" ]] && "$VPY" -c "import django, waitress, playwright" 2>/dev/null \
        || die "not set up yet -- run:  ./testhub.sh setup"
}

node_ok() {                           # Node.js 20 or newer on PATH?
    command -v node >/dev/null 2>&1 \
        && node -e 'process.exit(parseInt(process.versions.node, 10) >= 20 ? 0 : 1)' 2>/dev/null
}

ts_engine_ready() {
    [[ -x "${ENGINE}/node/bin/node" \
       && -f "${ENGINE}/engine/node_modules/@playwright/test/cli.js" ]]
}

load_config() {                       # sets HOST PORT PREFIX DATA HUB_URL TARGET TARGET_IS_DEMO
    local out
    out="$(cd "$APP" && "$VPY" - 2>&1 <<'PY'
from core import appconfig
try:
    c = appconfig.get_config()
except appconfig.ConfigError as exc:
    print("CONFIG-ERROR", exc)
    raise SystemExit(0)
host = c.site_host if c.site_host not in ("0.0.0.0", "::", "") else "127.0.0.1"
print(host)
print(c.site_port)
print(c.url_prefix)
print(c.data_dir)
print(c.public_url + c.url_prefix + "/")
print(c.target_url)
print(1 if c.target_is_demo else 0)
PY
)" || die "could not read the configuration: ${out}"
    case "$out" in
        *CONFIG-ERROR*) die "config.json has a problem: ${out#*CONFIG-ERROR }" ;;
    esac
    out="$(printf '%s\n' "$out" | tail -n 7)"
    {
        read -r HOST; read -r PORT; read -r PREFIX; read -r DATA
        read -r HUB_URL; read -r TARGET; read -r TARGET_IS_DEMO
    } <<< "$out"
}

port_in_use() {                       # does anything accept connections on HOST:PORT?
    "$VPY" - "$HOST" "$PORT" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(1)
raise SystemExit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
}

http_ok() {                           # does the URL answer 200?
    "$VPY" - "$1" <<'PY' >/dev/null 2>&1
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=3) as r:
    raise SystemExit(0 if r.status == 200 else 1)
PY
}

proc_cmd() {                          # the command line of a process
    if [[ -r "/proc/$1/cmdline" ]]; then
        tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null
    else
        ps -ww -p "$1" -o command= 2>/dev/null
    fi
}

proc_cwd() {                          # the working directory of a process
    if [[ -d "/proc/$1" ]]; then
        readlink "/proc/$1/cwd" 2>/dev/null
    else
        lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
    fi
}

is_our_hub() {                        # is this pid THIS folder's hub server?
    # By what it runs and where: macOS framework Pythons re-exec themselves
    # (the command line then names Python.app, not .venv/bin/python), but
    # the working directory -- this repo's app/ -- survives the exec.
    case "$(proc_cmd "$1")" in
        *"manage.py serve"*) ;;
        *) return 1 ;;
    esac
    [[ "$(proc_cwd "$1")" == "$APP" ]]
}

hub_pid() {                           # prints the pid of THIS repo's running hub
    local pid candidates
    if [[ -f "$PIDFILE" ]]; then
        pid="$(cat "$PIDFILE" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && is_our_hub "$pid"; then
            echo "$pid"; return 0
        fi
    fi
    # started another way (--foreground in another terminal), or the pid
    # file is gone: look for a hub server running in this app/ folder
    if [[ -d /proc/self ]]; then
        candidates="$(grep -l -a "manage.py" /proc/[0-9]*/cmdline 2>/dev/null \
                      | sed 's#^/proc/\([0-9]*\)/cmdline$#\1#')"
    else
        candidates="$(ps -ww -A -o pid= -o command= 2>/dev/null \
                      | awk '/manage\.py serve/ { print $1 }')"
    fi
    for pid in $candidates; do
        if is_our_hub "$pid"; then echo "$pid"; return 0; fi
    done
    return 1
}

seed_sample_tests() {                 # an empty tests folder + the demo as target:
    local tests="${DATA}/tests"       # the sample tests and groups go in, once
    if [[ -d "$tests" ]] && [[ -n "$(ls -A "$tests" 2>/dev/null)" ]]; then
        return 0
    fi
    if [[ "$TARGET_IS_DEMO" == "1" ]]; then
        say "first start: copying the sample tests into ${tests}"
        mkdir -p "$tests"
        cp -R "${APP}/sample_tests/." "$tests/"
        rm -rf "${tests}/__pycache__"
    else
        warn "no tests yet in ${tests} -- add your own (README: \"Writing tests\")"
    fi
}

# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
cmd_setup() {
    local py
    say "Python"
    if [[ -n "${PYTHON:-}" ]] && ! supported_python >/dev/null; then
        die "PYTHON=${PYTHON} is not Python 3.9-3.12 ($(py_version "$(command -v "$PYTHON" || echo "$PYTHON")" || true))"
    fi
    py="$(supported_python)" || die "no Python 3.9-3.12 found.
    Install one, then run setup again:
      macOS:   brew install python@3.12      (or the installer from python.org)
      Ubuntu:  sudo apt install python3.12 python3.12-venv
    Already have one somewhere else?  PYTHON=/path/to/python3.12 ./testhub.sh setup"
    if [[ -x "$VPY" ]] && ! "$VPY" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else 1)' 2>/dev/null; then
        die "${VENV} was made with an unsupported Python -- delete that folder and run setup again"
    fi
    if [[ ! -x "$VPY" ]]; then
        "$py" -m venv "$VENV" || die "could not create ${VENV} (on Ubuntu: sudo apt install python3-venv, or python3.12-venv)"
    fi
    ok "$("$VPY" --version) in .venv/"

    say "Python packages (requirements.txt)"
    "$VPY" -m pip install --quiet --upgrade pip
    "$VPY" -m pip install --quiet -r "${REPO}/requirements.txt"
    ok "Django $("$VPY" -c 'import django; print(django.get_version())'), Playwright $("$VPY" -m playwright --version | awk '{print $2}')"

    say "Chromium for the Python tests (a one-time download into .browsers/)"
    "$VPY" -m playwright install chromium
    local smoke
    if smoke="$("$VPY" - 2>&1 <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.set_content("<h1>ok</h1>")
    assert page.inner_text("h1") == "ok"
    browser.close()
PY
)"; then
        ok "Chromium starts and renders a page"
    else
        warn "Chromium did not start: $(why_not "$smoke")"
        warn "On Linux it needs some system libraries -- install them once with:"
        warn "  sudo ${VPY} -m playwright install-deps chromium"
    fi

    say "TypeScript engine (optional -- for the .spec.ts tests)"
    if ! node_ok; then
        warn "Node.js 20 or newer was not found, so TypeScript tests cannot run yet."
        warn "Python tests work without it. For TypeScript: install Node.js 22 LTS"
        warn "(nodejs.org, or: brew install node@22), then run ./testhub.sh setup again."
    else
        mkdir -p "${ENGINE}/node/bin" "${ENGINE}/engine" "${ENGINE}/browsers"
        ln -sf "$(command -v node)" "${ENGINE}/node/bin/node"
        cp "${REPO}/ts/package.json" "${REPO}/ts/package-lock.json" "${ENGINE}/engine/"
        if ! cmp -s "${REPO}/ts/package-lock.json" "${ENGINE}/engine/.installed-lock" 2>/dev/null \
           || [[ ! -f "${ENGINE}/engine/node_modules/@playwright/test/cli.js" ]]; then
            (cd "${ENGINE}/engine" && npm ci --no-audit --no-fund --loglevel=error)
            cp "${REPO}/ts/package-lock.json" "${ENGINE}/engine/.installed-lock"
        fi
        ok "Node.js $(node --version), @playwright/test $(node -p "require('${ENGINE}/engine/node_modules/@playwright/test/package.json').version")"
        PLAYWRIGHT_BROWSERS_PATH="${ENGINE}/browsers" \
            "${ENGINE}/node/bin/node" "${ENGINE}/engine/node_modules/@playwright/test/cli.js" install chromium
        if smoke="$(PLAYWRIGHT_BROWSERS_PATH="${ENGINE}/browsers" "${ENGINE}/node/bin/node" -e "
            const { chromium } = require('${ENGINE}/engine/node_modules/playwright-core');
            (async () => { const b = await chromium.launch(); await b.close(); })()
              .catch(e => { console.error(e.message); process.exit(1); });" 2>&1)"
        then
            ok "the TypeScript engine's Chromium starts"
        else
            warn "the TypeScript engine's Chromium did not start: $(why_not "$smoke")"
            warn "On Linux: sudo ${VPY} -m playwright install-deps chromium"
        fi
    fi

    echo
    say "${G}setup complete.${N}  Next:  ./testhub.sh start"
}

# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------
cmd_start() {
    local foreground=0 pid i
    case "${1:-}" in
        -f|--foreground) foreground=1 ;;
        "") ;;
        *) die "unknown option '$1' (did you mean --foreground?)" ;;
    esac
    need_setup
    load_config
    if pid="$(hub_pid)"; then
        ok "already running (pid ${pid}):  ${HUB_URL}"
        return 0
    fi
    if port_in_use; then
        die "port ${PORT} is already taken by another program.
    Stop that program, or give the hub another port in config.json:
      { \"site\": { \"port\": 8881 } }"
    fi
    ts_engine_ready || warn "TypeScript engine not installed -- .spec.ts tests will fail until you install Node.js 20+ and run ./testhub.sh setup"
    seed_sample_tests
    mkdir -p "$RUN_DIR"
    cd "$APP"

    if (( foreground )); then
        say "Test Hub on ${HUB_URL}  (Ctrl+C stops it)"
        echo $$ > "$PIDFILE"
        exec "$VPY" manage.py serve
    fi

    say "starting the Test Hub"
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') start" >> "$LOG"
    # background the python process itself (not a shell function), so $! is
    # the server's own pid and `stop` signals the right process
    nohup "$VPY" manage.py serve >> "$LOG" 2>&1 < /dev/null &
    pid=$!
    echo "$pid" > "$PIDFILE"
    for i in $(seq 1 90); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo; tail -n 30 "$LOG" >&2
            die "the Test Hub stopped during startup -- the log is above (full log: ${LOG})"
        fi
        if http_ok "http://${HOST}:${PORT}${PREFIX}/api/health/"; then
            ok "running (pid ${pid})"
            echo
            echo "  Test Hub     ${B}${HUB_URL}${N}"
            if [[ "$TARGET_IS_DEMO" == "1" ]]; then
                echo "  demo site    ${B}${HUB_URL}demo/freight/${N}   (Acme Freight -- served by the hub)"
            else
                echo "  testing      ${B}${TARGET}${N}"
            fi
            echo "  log          ${LOG}"
            echo "  stop it      ./testhub.sh stop"
            return 0
        fi
        sleep 1
    done
    die "the Test Hub did not answer within 90 seconds (still starting? see ./testhub.sh logs)"
}

cmd_stop() {
    local force=0 pid i
    [[ "${1:-}" == "--force" ]] && force=1
    need_setup
    load_config
    if ! pid="$(hub_pid)"; then
        rm -f "$PIDFILE"
        ok "not running"
        return 0
    fi
    say "stopping the Test Hub (pid ${pid}) -- any test still running is stopped cleanly"
    kill -TERM "$pid" 2>/dev/null || true
    for i in $(seq 1 90); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        if (( force )); then
            warn "still running after 90 s -- killing it"
            kill -KILL "$pid" 2>/dev/null || true
            sleep 1
        else
            die "still winding down after 90 s. Wait a little, or:  ./testhub.sh stop --force"
        fi
    fi
    rm -f "$PIDFILE"
    if port_in_use; then
        warn "port ${PORT} is still answering -- another program holds it"
    else
        ok "stopped (the demo site with it)"
    fi
}

cmd_status() {
    local pid engine
    need_setup
    load_config
    if ts_engine_ready; then engine="installed"; else engine="not installed (needs Node.js 20+, then ./testhub.sh setup)"; fi
    if pid="$(hub_pid)"; then
        ok "running (pid ${pid})  Test Hub $(cat "${APP}/VERSION")"
        echo "  Test Hub     ${HUB_URL}"
        echo "  testing      ${TARGET}"
    else
        echo "  not running   (start it: ./testhub.sh start)"
    fi
    echo "  TypeScript   ${engine}"
    echo "  data         ${DATA}   (tests, results, database)"
    echo "  log          ${LOG}"
}

cmd_logs() {
    [[ -f "$LOG" ]] || die "no log yet -- the hub has not been started with ./testhub.sh start"
    if [[ "${1:-}" == "-f" ]]; then
        tail -n 50 -f "$LOG"
    else
        tail -n 200 "$LOG"
    fi
}

# ---------------------------------------------------------------------------
# everything else
# ---------------------------------------------------------------------------
manage() {
    need_setup
    cd "$APP"
    exec "$VPY" manage.py "$@"
}

cmd_typecheck() {
    need_setup
    load_config
    ts_engine_ready || die "the TypeScript engine is not installed (Node.js 20+, then ./testhub.sh setup)"
    local tests="${DATA}/tests"
    [[ -d "$tests" ]] || die "no tests folder yet (${tests}) -- start the hub once"
    # the same link the hub makes before a TypeScript run: specs resolve
    # @playwright/test from the engine
    [[ -e "${tests}/node_modules" ]] || ln -s "${ENGINE}/engine/node_modules" "${tests}/node_modules"
    cd "$tests"
    local files
    files="$(ls ./*.spec.ts ./_lib/*.ts 2>/dev/null || true)"
    [[ -n "$files" ]] || { ok "no TypeScript tests to check"; return 0; }
    say "type-checking $(echo "$files" | wc -l | tr -d ' ') TypeScript file(s) in ${tests}"
    # shellcheck disable=SC2086
    "${ENGINE}/node/bin/node" "${ENGINE}/engine/node_modules/typescript/bin/tsc" --noEmit --strict \
        --target ES2022 --module commonjs --moduleResolution node --esModuleInterop \
        --skipLibCheck --types node $files
    ok "no type errors"
}

usage() {                             # the header comment of this file
    awk 'NR > 2 && /^# =====/ { exit } NR > 2 { sub(/^# ?/, ""); print }' "$0"
}

case "${1:-}" in
    setup)        shift; cmd_setup "$@" ;;
    start)        shift; cmd_start "$@" ;;
    stop)         shift; cmd_stop "$@" ;;
    restart)      shift; cmd_stop; cmd_start "$@" ;;
    status)       shift; cmd_status ;;
    logs)         shift; cmd_logs "$@" ;;
    run)          shift; [[ $# -gt 0 ]] || die "which tests?  e.g.  ./testhub.sh run DEMO-010 DEMO-T12"
                  manage run "$@" ;;
    demo-release) shift; manage demo_release "$@" ;;
    typecheck)    shift; cmd_typecheck ;;
    unit-tests)   shift; manage test core freight ;;
    manage)       shift; [[ $# -gt 0 ]] || die "which command?  e.g.  ./testhub.sh manage doctor"
                  manage "$@" ;;
    ""|-h|--help|help) usage ;;
    *)            usage; echo; die "unknown command '$1'" ;;
esac

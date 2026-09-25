# AGENTS.md — working in this repository

Guidance for AI coding agents (Codex, Claude Code, OpenCode, Cursor, …) and
for people. Read this before changing anything. The README is the user's
guide, and [docs/WRITING-TESTS.md](docs/WRITING-TESTS.md) is the full
test-writing reference.

## What this is

The **Test Hub**: a Django web app that writes, runs, schedules and analyses
Playwright browser tests in Python and TypeScript, with a built-in demo
website, **Acme Freight**, to test against. It is one process: the web
interface, the test runner, the weekly scheduler and the demo site all
start and stop together.

- **Files own the test definitions; the database owns the run history.**
  Tests are files in the tests folder. The hub reads them at start and on
  *Rescan*, and writes them back when they are edited in the web page.
- `app/` is a copy of the Test Hub from the offline-deployment project (its
  version is in `app/VERSION`). The same code runs on air-gapped RHEL 8 /
  CentOS 7 machines, which is where most of its constraints come from.

## Commands

Everything goes through `./testhub.sh`, run from the repository root:

| command | what it does |
|---|---|
| `./testhub.sh setup` | one time (safe to re-run): `.venv/`, `requirements.txt`, Chromium, and the TypeScript engine in `.pw-ts/` if Node.js 20+ is installed |
| `./testhub.sh start` / `stop` / `restart` / `status` | the hub, in the background; default http://127.0.0.1:8880/ |
| `./testhub.sh logs [-f]` | the server log (`.run/testhub.log`) |
| `./testhub.sh run ID [ID…]` | queue tests; prints `ID: queued as run N` |
| `./testhub.sh manage watch N` | follow run N to its end; the last line is `[run N] finished: PASSED (1.7s)` or `FAILED … -- <reason>` |
| `./testhub.sh manage new ID "Name" [--ts]` | scaffold a new test (file + settings) in the tests folder |
| `./testhub.sh manage sync` | rescan the tests folder after editing files |
| `./testhub.sh manage list` / `history ID` / `stats` | tests, one test's runs, pass rates |
| `./testhub.sh typecheck` | strict type-check of the TypeScript tests |
| `./testhub.sh unit-tests` | the hub's own unit suite: all must pass (4 checks of main-project files skip here, and say why) |
| `./testhub.sh manage doctor` | self-check, a fix named for each problem (all `OK` on a healthy machine while the hub runs) |
| `./testhub.sh demo-release [VER]` | list, or deploy, a release of the demo site (1.0.0 … 3.0.0) |

`curl -s http://127.0.0.1:8880/api/status/` is JSON of what is queued or
running (`"runs": []` means idle). `/api/health/` answers `{"ok": true, …}`.

`run` only QUEUES: the running hub executes the test, so start the hub
first. After editing `config.json`, `./testhub.sh restart`.

## The usual task: write or fix a test

1. **Where.** The tests folder is `data/tests/`. If `config.json` sets
   `paths.data_dir`, it is `<data_dir>/tests/` instead (`./testhub.sh status`
   prints the data folder). Names: `ID__name.py` (Python) or
   `ID__name.spec.ts` (TypeScript). The id is everything before `__` and
   must be unique. A `ID__name.json` beside it holds the name, description,
   tags, timeout and schedules (created automatically if missing). Shared
   code goes in `_lib/`: anything starting with `_` is importable and is
   never collected as a test.
2. **Python contract.** Define `run(page, ctx)`. `page` is a normal Playwright
   (Python 1.60) page. `ctx.base_url` is the target (never hard-code the
   site); `with ctx.timed("label"):` makes a named phase; `ctx.secret("name")`
   returns a credential; `ctx.screenshot("name")` and `ctx.log("text")` add
   to the run; `ctx.artifacts_dir` is the run's folder. A failed `assert` or
   any exception fails the test, and its message is what people read, so
   make it say what is wrong.
3. **TypeScript contract.** A standard `@playwright/test` spec (engine
   1.58.2). Navigate **relative to the target**: `page.goto('./')`,
   `page.goto('orders/42')`, **never** `page.goto('/')` (that is the server's
   root, not the target). `test.step()` blocks become the named phases.
   Credentials: `process.env.PW_SECRET_<NAME>` (upper case, non-alphanumerics
   become `_`).
4. **Verify it, don't just write it:**
   ```bash
   ./testhub.sh manage sync          # the hub now knows the new/changed file
   ./testhub.sh typecheck            # TypeScript only
   ./testhub.sh run ID               # -> "ID: queued as run N"
   ./testhub.sh manage watch N       # -> "[run N] finished: PASSED (…)"
   ```
   A failure's details are in `data/results/<ID>/<date-time>-r<N>/`:
   `run.log`, `result.json` (status, error, per-step timings), screenshots,
   `trace.zip`. The run page is http://127.0.0.1:8880/runs/N/. Run a new
   test two or three times before calling it done: flaky tests are worse
   than missing ones.
5. **Selectors and waits.** Prefer ids, roles and labels (`get_by_role`,
   `get_by_label`, `#stable-id`) over CSS paths. Assert that the page you
   reached is the right one (a login page or SSO redirect also has a title).
   Wait for conditions (`wait_for_selector`, `expect(…)`, `wait_for_url`),
   never a fixed sleep. Keep one user goal per test, and keep tests
   independent of each other: two run at a time.

Working models: `data/tests/_lib/freight.py` and `_lib/freight.ts` (page
objects) with DEMO-010…024 (Python) and DEMO-T12…T16 (TypeScript). Their
source is `app/sample_tests/`.

## Rules that are not negotiable

- **No credentials in files.** Tests travel through git, zips and discs, so
  a password written in one travels with it. Reference secrets by name
  (`ctx.secret("site_password")` / `process.env.PW_SECRET_SITE_PASSWORD`)
  and ask the human to store the value under *Settings → Credentials*. Never
  print a secret's value, and never pass one on a command line.
- **Point tests at test or staging environments.** Tests submit forms and
  change data. Aim them at production only with the human's explicit OK.
- **Don't bend the demo tests to a real site.** The DEMO-* samples are
  written for Acme Freight. A real site gets its own tests in its own data
  folder: `config.json` → `"target": {"url": …, "version": …}` plus
  `"paths": {"data_dir": "data-<name>"}`, then `./testhub.sh restart`.
- **Stop the hub with `./testhub.sh stop`**, never `kill -9` or
  `pkill python`. The graceful stop winds running tests down, records them,
  and leaves no stray browsers behind.
- **`manage.py runserver` is read-only**: it serves pages but never runs a
  test. Use `./testhub.sh start`.
- **Never commit** `data/`, `data-*/`, `config.json`, `.venv/`, `.browsers/`,
  `.pw-ts/` or `.run/`. They are per-machine and already in `.gitignore`.

## Changing the hub itself (`app/`)

`app/` mirrors a release of the Test Hub from the offline-deployment project
(`app/VERSION`), and a re-sync from there replaces it. Prefer making hub
changes upstream. When a patch has to land here, keep it small and say so in
the commit message, so the next sync doesn't silently drop it.

If you do edit `app/`, these constraints come from the machines it ships to:

| constraint | why |
|---|---|
| Python **3.9** syntax and stdlib (no `match`, no `X \| Y` unions) | the lab runs CPython 3.9.25 |
| Django **4.2** only | the last Django that supports Python 3.9 |
| new dependencies: pure-Python wheels only (`*-none-any`) | one app zip serves both Linux distributions |
| no external URLs anywhere in the UI | the lab has no internet; JS/CSS libraries are vendored in `app/core/static/core/vendor/` |
| JSON goes in `TextField`s, never `JSONField` | no dependence on SQLite's JSON1 extension |
| the harnesses (`app/core/harness/`) stay Django-free and Playwright-1.35-compatible | CentOS 7 runs Playwright 1.35 |
| never hard-code an app path: `{% url %}` / `reverse()` in Python, `hub.u("/api/…")` in JavaScript | the hub can be served under a URL prefix (`/testhub/`) |
| queries over a test's runs use `Run.regular()` | load-test iterations must not leak into ordinary views and charts |
| new Django templates: append blocks at the end; never replace the first `{% endblock %}` (that is the title block) | |

After a change:
- run `./testhub.sh unit-tests` (all must pass);
- run `./testhub.sh restart` (the server caches Python code and templates;
  CSS and JS are read fresh);
- run a few sample tests in both languages, e.g.
  `./testhub.sh run DEMO-010 DEMO-T12`, then watch them.

Map of the code: `app/core/services/` (runner, scheduler, syncer, stats,
analytics, load testing, storage, diagnostics), `app/core/harness/`
(`run_test.py` for Python tests, `run_ts_test.py` for TypeScript tests,
`security.py`), `app/core/views.py` + `app/core/api.py` + templates for the
UI, `app/freight/` for the demo site (its five releases are defined in
`freight/releases.py`), and `app/core/tests.py` + `app/freight/tests.py`
for the unit tests.

## Maintaining this repository

**Updating `app/` from upstream.** Copy a release of the main project's
`app/` over this one, then check that nothing else differs:
```bash
rsync -a --delete --exclude __pycache__ --exclude '*.pyc' --exclude .DS_Store \
      --exclude dev.sh --exclude 'requirements*.txt' --exclude '*.before-*' \
      <main project>/app/ app/
```
Then update the version named in the README. `docs/TESTHUB.md` is the
upstream manual plus two local edits, which must be re-applied after
copying it: the "Reading this in the laptop edition" note after its
introduction, and its `docs/AWS.md` link turned into plain text (that file
is not in this repository). Finish with `./testhub.sh unit-tests` and a
Python + TypeScript sample run.

**`testhub.sh` rules, each learned the hard way:**
- It must run on **bash 3.2** (macOS `/bin/bash`): no associative arrays,
  no `mapfile`, no `${var,,}`, and never expand a possibly-empty array
  under `set -u`. Check with `/bin/bash -n testhub.sh`, then really run it
  with `/bin/bash`.
- **The hub's process is identified by its command AND its working
  directory**, never by its command line alone. Homebrew's framework Python
  re-executes itself as `…/Python.app/Contents/MacOS/Python`, so the
  `.venv` path never shows in `ps`; macOS `ps` truncates long lines without
  `-ww`; and a text search through `ps` output also matches the searching
  process itself. The working directory (`lsof` on macOS, `/proc` on Linux)
  is this repo's `app/`, from `pwd -P`.
- Background the Python executable itself (`nohup "$VPY" manage.py serve &`),
  never a shell function, so `$!` is the server and `stop` signals the
  right process.
- Setup launches each browser once to prove it works, and on failure prints
  ONE line of reason plus the fix (`sudo .venv/bin/python -m playwright
  install-deps chromium` on Linux), not a traceback.

**Test a change on both platforms before calling it done:** macOS (with
`/bin/bash`) and Linux. For Linux on a Mac, Docker with
`node:22-bookworm` (Debian 12, Python 3.11, Node 22) or
`python:3.12-bookworm` works. colima shares only your home folder with its
VM, so stream a clean copy in rather than bind-mounting a temp folder:
```bash
{ git ls-files; git ls-files -o --exclude-standard; } | tar -cf - -T - \
  | docker run -i --rm node:22-bookworm bash -c \
      'mkdir /w && tar -xf - -C /w && cd /w && apt-get update -qq && apt-get install -y -qq python3-venv \
       && ./testhub.sh setup && .venv/bin/python -m playwright install-deps chromium \
       && ./testhub.sh setup && ./testhub.sh start && ./testhub.sh run DEMO-010 DEMO-T12 \
       && ./testhub.sh manage watch 1 && ./testhub.sh manage watch 2 && ./testhub.sh stop'
```
Both runs must end `finished: PASSED`. Also try a fresh copy on the Mac with
another Python (`PYTHON=python3.9 ./testhub.sh setup`) and another port
(`config.json`).

**Docs.** Every command, path, UI label and test id quoted in a `.md` file
must exist. Check them against `testhub.sh`, `app/core/management/commands/`,
the templates and `app/sample_tests/`, and check tables and anchors with
GitHub's own renderer (`POST https://api.github.com/markdown`). README
screenshots come from a clean history: reset `data/`, run the demo story
across its releases, then capture them (`docs/images/`).

## When something is wrong

| symptom | look at |
|---|---|
| anything | `./testhub.sh manage doctor`, then `./testhub.sh logs` |
| `start` says the port is taken | another program has it; set `"site": {"port": …}` in `config.json` |
| a TS test errors with *the TS engine is not installed* | Node.js 20+ and `./testhub.sh setup` |
| Chromium won't start on Linux | `sudo .venv/bin/python -m playwright install-deps chromium` |
| a test passes locally but fails in the hub | the hub runs headless, two tests at a time, against `target.url`: compare the base URL and look for shared test data |
| DEMO-017 fails after deploying demo 2.0.0+ | expected (the redesign); delete `data/tests/DEMO-017__baseline.png` to approve the new look |

## Documentation

- [README.md](README.md): what it is, setup, start and stop, the demo site,
  pointing it at a real application, requirements.
- [docs/WRITING-TESTS.md](docs/WRITING-TESTS.md): every contract, file and
  key for writing tests.
- [docs/TESTHUB.md](docs/TESTHUB.md): the full manual, written for lab
  installs (its first lines map lab paths to this repository).
- [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md): vendored components and
  their licences.

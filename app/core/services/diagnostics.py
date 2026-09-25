"""Self-diagnosis: one place that answers "why isn't this working?" on a
machine nobody can SSH into quickly and where re-installing is a trip.

Every check returns ok | warn | fail plus a sentence a non-expert can act
on. Exposed three ways: Settings page button, `testhub doctor` (works even
when the web server will not start), and /api/doctor/.
"""
import os
import shutil
import sys
from pathlib import Path

from core import appconfig

OK, WARN, FAIL = "ok", "warn", "fail"

DISK_WARN_MB = 2000
DISK_FAIL_MB = 500


def _check(name, status, detail, fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _package_version(name: str) -> str:
    """An installed package's version from its metadata (Playwright has no
    __version__ attribute to read)."""
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "unknown"


def _free_mb(path: Path) -> "float | None":
    try:
        return shutil.disk_usage(str(path)).free / 1e6
    except OSError:
        return None


def run_checks(quick=False) -> list:
    """quick=True skips the browser launch (~2 s) for page loads that just
    want a status strip."""
    cfg = appconfig.get_config()
    out = []

    # --- interpreter ---------------------------------------------------------
    out.append(_check("Python", OK,
                      f"{sys.version.split()[0]} at {sys.executable}"))

    # --- app version ---------------------------------------------------------
    out.append(_check("Test Hub version", OK, app_version()))

    # --- playwright + browsers ----------------------------------------------
    try:
        import playwright  # noqa: F401 -- importable is the check
        pw_version = _package_version("playwright")
        out.append(_check("Playwright library", OK, f"version {pw_version}"))
    except ImportError as exc:
        out.append(_check("Playwright library", FAIL, str(exc),
                          "Re-run install-app.sh (its wheels are cached at "
                          "/opt/pw-testhub/wheels)"))
        pw_version = None

    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if browsers_path and Path(browsers_path).is_dir():
        n = len([p for p in Path(browsers_path).iterdir() if p.is_dir()])
        out.append(_check("Bundled browsers", OK if n else FAIL,
                          f"{n} browser build(s) in {browsers_path}",
                          "" if n else "The base bundle was installed with "
                                       "--no-browsers, or the folder was removed"))
    else:
        out.append(_check("Bundled browsers", WARN,
                          "PLAYWRIGHT_BROWSERS_PATH is not set — Playwright will "
                          "look in the user cache and may find nothing",
                          "Start the hub through /opt/pw-testhub/bin/testhub, "
                          "which sources the bundle's env.sh"))

    if not quick and pw_version:
        out.append(_browser_launch_check(cfg))

    # --- database ------------------------------------------------------------
    try:
        import sqlite3
        from django.db import connection
        ver = sqlite3.sqlite_version
        status = OK if tuple(int(x) for x in ver.split(".")) >= (3, 21, 0) else FAIL
        connection.ensure_connection()
        with connection.cursor() as cur:
            cur.execute("select count(*) from core_run")
            runs = cur.fetchone()[0]
        out.append(_check("Database", status,
                          f"SQLite {ver}, {runs} run(s) recorded",
                          "" if status == OK else "SQLite older than 3.21 cannot "
                                                  "run Django 4.2 — rebuild the base bundle"))
    except Exception as exc:
        out.append(_database_error_check(exc))

    # --- pending migrations ---------------------------------------------------
    try:
        from django.db import connection as conn
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(conn)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        out.append(_check("Database schema", OK if not pending else WARN,
                          "up to date" if not pending
                          else f"{len(pending)} migration(s) not applied",
                          "" if not pending else "Run: testhub migrate"))
    except Exception as exc:
        out.append(_check("Database schema", WARN, str(exc)[:150]))

    # --- writable dirs + disk -------------------------------------------------
    for label, path in (("Tests folder", cfg.tests_dir),
                        ("Results folder", cfg.results_dir)):
        if os.access(path, os.W_OK):
            n = len(list(path.glob("*.py"))) if label.startswith("Tests") else None
            out.append(_check(label, OK,
                              f"{path}" + (f" — {n} test file(s)" if n is not None else "")))
        else:
            out.append(_check(label, FAIL, f"{path} is not writable",
                              "Fix ownership: chown -R the data directory to the "
                              "user running the hub"))

    free = _free_mb(cfg.data_dir)
    if free is None:
        out.append(_check("Disk space", WARN, "could not be determined"))
    else:
        status = OK if free > DISK_WARN_MB else (WARN if free > DISK_FAIL_MB else FAIL)
        out.append(_check("Disk space", status, f"{free/1000:.1f} GB free where runs are stored",
                          "" if status == OK else
                          "Prune old run artifacts on this page, or set "
                          "maintenance.retention_days"))

    # --- timezone -------------------------------------------------------------
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(cfg.timezone)
        out.append(_check("Schedule timezone", OK, cfg.timezone))
    except Exception as exc:
        out.append(_check("Schedule timezone", FAIL,
                          f"{cfg.timezone} is not a valid timezone ({exc})",
                          "Set a name like America/Los_Angeles in Settings"))

    # --- secrets on a shared machine -------------------------------------------
    # These boxes have more than one login. A readable .secret_key lets anyone
    # forge a session cookie and walk past the password gate; a readable
    # config.json hands them the gate password directly. Both are one chmod to
    # fix and invisible until someone looks, so doctor looks.
    import stat as _stat
    loose = []
    secret = cfg.data_dir / ".secret_key"
    for path, why in ((secret, "session keys can be forged"),
                      (cfg.path, "the site password is in it")):
        try:
            if path.exists() and _stat.S_IMODE(path.stat().st_mode) & 0o077:
                if path == cfg.path and not cfg.raw.get("site", {}).get("password"):
                    continue        # no password stored, nothing to leak
                loose.append((path, oct(_stat.S_IMODE(path.stat().st_mode)), why))
        except OSError:
            continue
    if loose:
        out.append(_check(
            "File permissions", WARN,
            "; ".join(f"{p.name} is {mode} — {why}" for p, mode, why in loose),
            "chmod 600 " + " ".join(str(p) for p, _, _ in loose)))
    elif secret.exists():
        out.append(_check("File permissions", OK,
                          "secret key and config are not world-readable"))

    out.append(sleep_check(cfg))

    # --- fapolicyd (application allow-listing on hardened RHEL 8) -------------
    fap = fapolicyd_check()
    if fap:
        out.append(fap)

    # --- the bundled AI agent --------------------------------------------------
    oc = opencode_check()
    if oc:
        out.append(oc)

    # --- runner / scheduler ----------------------------------------------------
    from core.services import runner as runner_mod
    if runner_mod.get() is not None:
        out.append(_check("Runner", OK, "active — tests can run and schedules can fire"))
    else:
        out.append(serving_hub_check(cfg))

    # --- target reachability ----------------------------------------------------
    out.append(target_check(cfg))
    return out


def sleep_check(cfg, platform=None):
    """Will this machine sleep through its own schedule? Worth a check
    because the symptom is so misleading: a box that suspends at 01:50
    misses the 02:00 schedule, and it looks like a broken scheduler."""
    platform = platform or sys.platform
    try:
        from core.services import keepawake
        if platform == "darwin":
            # the lock is systemd's (Linux); a Mac is somebody's laptop
            return _check("Sleep prevention", OK,
                          "not managed on macOS -- for schedules that must fire "
                          "while nobody is at the Mac, keep it awake (System "
                          "Settings > Battery/Energy, or `caffeinate -s` in a "
                          "terminal)")
        if not cfg.keep_awake:
            return _check("Sleep prevention", WARN,
                          "switched off (runner.keep_awake is false)",
                          "Scheduled runs will be missed if this machine "
                          "suspends. Turn it on, or mask the sleep targets.")
        if keepawake.can_inhibit():
            state = keepawake.status()
            return _check("Sleep prevention", OK,
                          "a sleep inhibitor can be taken while tests run"
                          + (" (held right now)" if state["held"] else ""))
        return _check("Sleep prevention", WARN,
                      "this machine cannot take a sleep-inhibitor lock",
                      "Run 'caffeinate --status' to see whether it can suspend at "
                      "all. On a server that must run overnight: sudo systemctl mask "
                      "sleep.target suspend.target hibernate.target hybrid-sleep.target")
    except Exception as exc:
        return _check("Sleep prevention", WARN, str(exc)[:120])


def serving_hub_check(cfg):
    """Is a hub SERVING -- running tests and firing schedules? `testhub
    doctor` is its own process with no runner in it, so it asks the serving
    hub's health endpoint (the one URL outside the password gate) instead
    of reporting on itself: a healthy machine used to get a warning here."""
    import json
    import urllib.request
    host = cfg.site_host if cfg.site_host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    base = f"http://{host}:{cfg.site_port}{cfg.url_prefix}"
    try:
        with urllib.request.urlopen(f"{base}/api/health/", timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return _check("Runner", WARN,
                      f"no Test Hub is serving at {base}/ — tests cannot run "
                      f"and schedules cannot fire",
                      "Start it with 'testhub serve' (or its service)")
    if body.get("runner"):
        return _check("Runner", OK, f"the hub serving at {base}/ runs tests "
                                    f"and fires schedules")
    return _check("Runner", WARN,
                  f"the hub at {base}/ serves pages only (read-only UI)",
                  "Start the hub with 'testhub serve', not runserver")


def _browser_launch_check(cfg):
    """The check that matters most: can a browser actually start here?"""
    import subprocess
    code = (
        "import sys\n"
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as pw:\n"
        f"    b = pw.chromium.launch(**({{'channel': '{cfg.browser}'}} "
        f"if '{cfg.browser}' in ('chrome', 'msedge') else {{}}))\n"
        "    v = b.version\n"
        "    b.close()\n"
        "print(v)\n"
    )
    python = cfg.harness_python or sys.executable
    try:
        proc = subprocess.run([python, "-c", code], capture_output=True,
                              text=True, timeout=90)
    except Exception as exc:
        return _check("Browser launch", FAIL, str(exc)[:200])
    if proc.returncode == 0:
        return _check("Browser launch", OK,
                      f"{cfg.browser} started — version {proc.stdout.strip()}")
    err = (proc.stderr or "").strip().splitlines()
    return _check("Browser launch", FAIL,
                  err[-1][:250] if err else "browser did not start",
                  "Check the base bundle's RPMs installed (missing system "
                  "libraries are the usual cause) — see /tmp/pw-offline-rpm.log")


def target_check(cfg=None):
    import urllib.error
    import urllib.request
    cfg = cfg or appconfig.get_config()
    url = cfg.target_url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pw-testhub-doctor"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return _check("Target reachable", OK, f"HTTP {resp.status} from {url}")
    except urllib.error.HTTPError as exc:
        return _check("Target reachable", OK,
                      f"HTTP {exc.code} from {url} (reachable)")
    except Exception as exc:
        return _check("Target reachable", FAIL,
                      f"{url}: {type(exc).__name__}: {exc}",
                      "The hub's browsers reach the target FROM THIS MACHINE — "
                      "check firewall/proxy/target.url in Settings")


def _database_error_check(exc, euid=None) -> dict:
    """Turn a database error into an honest doctor row.

    A permission error for a NON-ROOT user is not a broken database: the hub
    deliberately keeps db.sqlite3 at 0600 (its session table holds login
    cookies), so an unprivileged `testhub doctor` cannot open it on a
    service install. Reporting that as FAIL reads like corruption and sends
    someone hunting a problem that does not exist."""
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else 0
    text = str(exc)
    denied = (isinstance(exc, PermissionError)
              or "unable to open database file" in text
              or "attempt to write a readonly database" in text)
    if denied and euid != 0:
        return _check("Database", WARN,
                      "not readable by this user — db.sqlite3 is kept 0600 "
                      "on purpose (its session table holds login cookies)",
                      "Run with sudo for the full check: sudo "
                      "/opt/pw-testhub/bin/testhub doctor")
    return _check("Database", FAIL, text[:200],
                  "Check that the data directory is writable")


def _offline_prefix() -> Path:
    """Where the base bundle lives. env.sh exports this; the default matches
    a standard install."""
    return Path(os.environ.get("PW_OFFLINE_PREFIX", "/opt/pw-offline"))


# RPM-installed browsers/webdrivers a test run can execute. The rpm trust
# database should cover them, but its cache can lag installs done while the
# daemon was stopped — the installers allow-list these explicitly, and
# doctor verifies the ones that exist on this box are in the list.
BROWSER_DIRS = (Path("/opt/google/chrome"), Path("/opt/microsoft/msedge"),
                Path("/usr/lib64/chromium-browser"), Path("/usr/lib64/firefox"))


def fapolicyd_check(etc=Path("/etc/fapolicyd"), prefix=None, app_home=None,
                    browser_dirs=None):
    """None when fapolicyd is not on the box (CentOS 7, dev laptops) — no row.

    fapolicyd (hardened RHEL 8) refuses to execute anything it does not
    trust, and the whole runtime — bundled python, its .so files, browsers,
    node — is exactly that until the installer's allow-list is in place. The
    failure mode is vicious: the hub keeps running (it is already executing),
    but every NEW harness subprocess dies with 'operation not permitted',
    which reads as a broken runner rather than a security policy. So doctor
    looks.
    """
    if not etc.is_dir():
        return None
    prefix = prefix or _offline_prefix()
    app_home = app_home or appconfig.APP_DIR.parent
    if browser_dirs is None:
        browser_dirs = [d for d in BROWSER_DIRS if d.is_dir()]
    rules = etc / "rules.d" / "10-pw-offline.rules"

    import subprocess
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "fapolicyd"],
            timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        active = False

    try:
        text = rules.read_text()
    except OSError:
        text = ""
    # the installers always write dir= lines with a trailing slash
    required = [prefix, app_home] + list(browser_dirs)
    missing = [str(d) for d in required if f"dir={d}/" not in text]

    fix_install = ("Re-run the kit's install-all.sh (it works with NO zips "
                   f"beside it) — that writes {rules} and restarts fapolicyd")
    if active and not missing:
        return _check("fapolicyd", OK,
                      "enforcing, and the runtime, hub and browsers are "
                      "allow-listed")
    if active:
        return _check("fapolicyd", WARN,
                      "enforcing, but " + ", ".join(missing) + " " +
                      ("is" if len(missing) == 1 else "are") + " not in its "
                      "allow-list — test runs will fail with 'operation "
                      "not permitted'", fix_install)
    if missing:
        return _check("fapolicyd", WARN,
                      "installed but not running — and the allow-list is "
                      "missing, so starting it would break test runs",
                      fix_install)
    return _check("fapolicyd", WARN,
                  "installed but not running (stopped for an install and "
                  "never restarted?)",
                  "The allow-list is already staged — start it again: "
                  "sudo systemctl start fapolicyd")


def opencode_check(prefix=None):
    """None when no base bundle is on this machine (dev laptops) — no row.

    'opencode: command not found' in a shell is nearly always a mistyped
    `source .../env.sh` line (which fails silently and takes playwright and
    robot down with it), so say exactly where the binary is and what puts it
    on PATH.
    """
    prefix = prefix or _offline_prefix()
    if not prefix.is_dir():
        return None
    binary = prefix / "bin" / "opencode"
    if binary.is_file() and os.access(str(binary), os.X_OK):
        return _check("OpenCode (AI agent)", OK,
                      f"installed at {binary} — shells see it after: "
                      f"source {prefix}/env.sh (add to ~/.bashrc, copied "
                      "exactly)")
    return _check("OpenCode (AI agent)", WARN,
                  f"not installed at {binary}",
                  "The base bundle ships it when its build-time probe passed "
                  "— check opencode-probe.txt in the bundle zip, and re-run "
                  "the base install if it should be there")


def app_version() -> str:
    path = appconfig.APP_DIR / "VERSION"
    try:
        return path.read_text().strip()
    except OSError:
        return "unknown"


def summary(checks) -> dict:
    return {
        "ok": sum(1 for c in checks if c["status"] == OK),
        "warn": sum(1 for c in checks if c["status"] == WARN),
        "fail": sum(1 for c in checks if c["status"] == FAIL),
        "healthy": not any(c["status"] == FAIL for c in checks),
    }

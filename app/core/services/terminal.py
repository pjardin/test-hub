"""Shared plumbing for the terminal commands (`testhub run|watch|kill|...`)
and the curses TUI.

The contract that makes "queue it, log out, it keeps running" true: these
helpers only WRITE INTENT -- Run rows with trigger='cli' (adopted by the
serving process's runner within ~2s), the kill flag, sidecar edits. The
serve process (systemd or foreground) is the only thing that executes.
"""
import json
import os
import socket
import subprocess
import time

from django.utils import timezone

from core import appconfig
from core.models import Run, Test
from core.services import syncer


# --- service detection -------------------------------------------------------

def service_state():
    """Is a hub serving on this machine? Checked the dumb reliable way: can
    the configured port be connected to."""
    cfg = appconfig.get_config()
    host = cfg.site_host if cfg.site_host not in ("0.0.0.0", "::") else "127.0.0.1"
    try:
        with socket.create_connection((host, cfg.site_port), timeout=1.5):
            return {"running": True, "port": cfg.site_port}
    except OSError:
        return {"running": False, "port": cfg.site_port}


def warn_if_no_service(out):
    st = service_state()
    if not st["running"]:
        out("")
        out("NOTE: the hub is not serving on port %s -- queued runs and" % st["port"])
        out("      schedules wait until it is. Start it (it outlives your login):")
        out("          sudo systemctl start pw-testhub     (installed service)")
        out("          testhub serve                       (foreground)")


# --- enqueue / kill ----------------------------------------------------------

def enqueue_cli(tests, browser=""):
    """Create QUEUED rows the serving runner adopts. Returns the runs."""
    from core.models import Batch
    cfg = appconfig.get_config()
    tests = [t for t in tests if t.enabled and not t.archived]
    if not tests:
        raise ValueError("nothing to run: no enabled tests in the selection")
    label = tests[0].test_id if len(tests) == 1 else f"{len(tests)} tests (terminal)"
    batch = Batch.objects.create(label=label, trigger="terminal")
    runs = []
    for test in tests:
        runs.append(Run.objects.create(
            test=test, batch=batch, trigger="cli",
            target_url=cfg.target_url, target_version=cfg.target_version,
            browser=browser or cfg.browser, headed=False,
        ))
    return runs


def kill_from_terminal(run_id):
    from core.services.runner import kill_run
    return kill_run(run_id)


# --- presentation helpers ----------------------------------------------------

def fmt_dur(seconds):
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def pad(text, width):
    text = str(text)
    return text[:width].ljust(width)


def test_rows():
    """One line of truth per test, with honest stats (Run.regular() only --
    load iterations never belong in a terminal listing either)."""
    rows = []
    for t in Test.objects.filter(archived=False).order_by("test_id"):
        qs = Run.regular().filter(test=t, status__in=Run.FINISHED_STATUSES)
        rep = qs.filter(status__in=Run.REPRESENTATIVE_STATUSES)
        passed = rep.filter(status=Run.PASSED).count()
        rep_n = rep.count()
        durs = sorted(r.duration_seconds for r in rep
                      if r.duration_seconds is not None)
        median = durs[len(durs) // 2] if durs else None
        last = qs.order_by("-id").first()
        rows.append({
            "test": t,
            "runs": qs.count(),
            "pass_rate": (round(100 * passed / rep_n) if rep_n else None),
            "median_s": median,
            "last_status": last.status if last else None,
            "last_when": (timezone.localtime(last.finished_at).strftime("%Y-%m-%d %H:%M")
                          if last and last.finished_at else ""),
            "schedules": t.schedules.count(),
        })
    return rows


def active_runs():
    return list(Run.objects.filter(status__in=Run.ACTIVE_STATUSES)
                .select_related("test").order_by("id"))


def log_path_for(run):
    cfg = appconfig.get_config()
    if not run.artifacts_rel:
        return None
    return cfg.results_dir / run.artifacts_rel / "run.log"


# --- sidecar schedules (files own definitions; edits write files back) ------

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_schedule_words(spec):
    """'mon 02:00' | 'daily 14:30' -> sidecar entry dict, or None."""
    parts = str(spec).split()
    if len(parts) != 2:
        return None
    day, hhmm = parts[0].lower(), parts[1]
    try:
        hh, mm = hhmm.split(":")
        hh, mm = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    if day == "daily":
        days = list(DAY_KEYS)
    elif day in DAY_KEYS:
        days = [day]
    else:
        return None
    return {"days": days, "time": f"{hh:02d}:{mm:02d}", "enabled": True}


def add_schedule(test, spec):
    entry = parse_schedule_words(spec)
    if entry is None:
        return None
    cfg = appconfig.get_config()
    path = syncer.sidecar_path(cfg.tests_dir / test.file_path)
    try:
        meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        meta = {}
    scheds = meta.get("schedules") or []
    scheds.append(entry)
    meta["schedules"] = scheds
    syncer._atomic_write(path, json.dumps(meta, indent=2) + "\n")
    syncer.sync_from_files()
    return entry


def clear_schedules(test):
    cfg = appconfig.get_config()
    path = syncer.sidecar_path(cfg.tests_dir / test.file_path)
    try:
        meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        meta = {}
    meta["schedules"] = []
    syncer._atomic_write(path, json.dumps(meta, indent=2) + "\n")
    syncer.sync_from_files()


# --- scaffold / edit ---------------------------------------------------------

def scaffold(test_id, name, ts=False):
    cfg = appconfig.get_config()
    # same id rules as the web form -- the id becomes part of a filename,
    # so this is also what keeps `testhub new` inside the tests dir
    test_id = syncer.validate_test_id(test_id)
    if ts:
        filename = syncer.make_filename_ts(test_id, name)
        code = syncer.NEW_TS_TEST_TEMPLATE.format(
            name=syncer.ts_string(name or test_id))
    else:
        filename = syncer.make_filename(test_id, name)
        code = syncer.NEW_TEST_TEMPLATE.format(
            name=syncer.docstring_safe(name or test_id))
    path = cfg.tests_dir / filename
    if path.exists():
        raise FileExistsError(filename)
    syncer._atomic_write(path, code)
    sidecar = {
        "id": test_id, "name": name or test_id, "description": "",
        "tags": [], "version": "", "timeout_seconds": 0,
        "enabled": True, "schedules": [],
    }
    syncer._atomic_write(syncer.sidecar_path(path),
                         json.dumps(sidecar, indent=2) + "\n")
    syncer.sync_from_files()
    return filename


def open_in_editor(test):
    cfg = appconfig.get_config()
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    path = cfg.tests_dir / test.file_path
    subprocess.call([editor, str(path)])
    return syncer.sync_from_files()

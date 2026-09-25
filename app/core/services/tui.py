"""The Python hub's full-screen terminal UI (curses -- stdlib only, so it
works on every lab box with zero extra wheels).

The dashboard over SSH, for the weeks before the web port is reachable:
tests with last results, live activity, one-key actions. Everything it
starts or schedules keeps running after you quit -- the SERVING hub owns
the work; this UI only writes intent (the same rows and sidecar files the
web UI writes).

Keys:  r run   a run all   k kill   w log   s schedule   e edit
       n new (N = new TypeScript)   R rescan   q quit
"""
import curses
import subprocess
import time

from django.utils import timezone

from core.models import Run, Test
from core.services import syncer, terminal


def _clip(win, y, x, text, attr=0):
    height, width = win.getmaxyx()
    if y >= height or x >= width:
        return
    try:
        win.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


def _prompt(stdscr, label):
    """One-line input at the bottom of the screen. Empty string = cancel."""
    height, width = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    _clip(stdscr, height - 1, 0, label + " ")
    stdscr.refresh()
    try:
        raw = stdscr.getstr(height - 1, len(label) + 1, 80)
        return raw.decode("utf-8", "replace").strip()
    except (curses.error, KeyboardInterrupt):
        return ""
    finally:
        curses.noecho()
        curses.curs_set(0)


def _run_editor(stdscr, test):
    """Leave curses, run $EDITOR, come back, rescan."""
    curses.endwin()
    try:
        summary = terminal.open_in_editor(test)
    finally:
        stdscr.refresh()
    errs = summary.get("errors") or []
    return "rescanned" + (f" ({len(errs)} problem(s))" if errs else "")


def _tail(path, lines=200):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data.decode("utf-8", "replace").splitlines()[-lines:]
    except OSError:
        return ["(no log yet)"]


def _log_view(stdscr, run):
    """Scrollable, self-refreshing log overlay. w/q/Esc closes."""
    stdscr.nodelay(True)
    try:
        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            run.refresh_from_db()
            path = terminal.log_path_for(run)
            lines = _tail(path, height - 3) if path else ["(no artifacts yet)"]
            _clip(stdscr, 0, 0,
                  f" run {run.pk} [{run.status}] log -- w/q/Esc closes ",
                  curses.A_REVERSE)
            for i, line in enumerate(lines[-(height - 2):]):
                _clip(stdscr, i + 1, 0, line)
            stdscr.refresh()
            for _ in range(15):   # ~1.5s, but responsive to keys
                ch = stdscr.getch()
                if ch in (ord("w"), ord("q"), 27):
                    return
                time.sleep(0.1)
    finally:
        stdscr.nodelay(False)


def _main(stdscr):
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.timeout(2000)   # refresh every 2s, react to keys instantly
    selected = 0
    flash = ""
    flash_at = 0.0

    while True:
        rows = terminal.test_rows()
        act = terminal.active_runs()
        selected = max(0, min(selected, len(rows) - 1)) if rows else 0
        st = terminal.service_state()

        height, width = stdscr.getmaxyx()
        stdscr.erase()
        head = (" Test Hub (terminal)  service: "
                + (f"SERVING :{st['port']}" if st["running"]
                   else "NOT SERVING -- systemctl start pw-testhub"))
        if flash and time.time() - flash_at < 4:
            head += f"   {flash}"
        _clip(stdscr, 0, 0, head.ljust(width - 1), curses.A_REVERSE)

        list_h = max(3, height - 7 - min(4, len(act)))
        _clip(stdscr, 1, 0, " ID            TYP  NAME                          "
                            "RUNS  PASS%  SCHED  LAST", curses.A_BOLD)
        top = max(0, selected - (list_h - 1))
        for i, r in enumerate(rows[top:top + list_h]):
            idx = top + i
            t = r["test"]
            line = (terminal.pad(t.test_id, 14)
                    + terminal.pad("ts" if t.test_type == "ts" else "py", 5)
                    + terminal.pad(t.display_name, 30)
                    + terminal.pad(r["runs"], 6)
                    + terminal.pad("-" if r["pass_rate"] is None else f"{r['pass_rate']}%", 7)
                    + terminal.pad(r["schedules"] or "-", 7)
                    + (r["last_status"] or "never"))
            if not t.enabled:
                line += "  [disabled]"
            _clip(stdscr, 2 + i, 0, line,
                  curses.A_REVERSE if idx == selected else 0)

        y = 2 + min(list_h, max(1, len(rows)))
        _clip(stdscr, y, 0, " Activity", curses.A_BOLD)
        if not act:
            _clip(stdscr, y + 1, 0, "   idle")
            y += 2
        else:
            for i, run in enumerate(act[:4]):
                _clip(stdscr, y + 1 + i, 0,
                      f"   #{run.pk} {terminal.pad(run.test.test_id, 14)}{run.status}")
            y += 1 + min(4, len(act))

        _clip(stdscr, height - 2, 0,
              " r run  a run all  k kill  w log  s schedule  e edit  "
              "n new  N new-TS  R rescan  q quit", curses.A_DIM)
        _clip(stdscr, height - 1, 0,
              " runs + schedules keep going after you quit -- the hub service owns them",
              curses.A_DIM)
        stdscr.refresh()

        ch = stdscr.getch()
        cur = rows[selected]["test"] if rows else None
        if ch in (ord("q"), 3):
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            selected += 1
        elif ch == curses.KEY_UP:      # no vim 'k' here: plain k is Kill
            selected -= 1
        elif ch == curses.KEY_PPAGE:
            selected -= 10
        elif ch == curses.KEY_NPAGE:
            selected += 10
        elif ch == ord("r") and cur is not None:
            try:
                runs = terminal.enqueue_cli([cur])
                flash = f"queued {cur.test_id} as run {runs[0].pk}"
            except ValueError as exc:
                flash = str(exc)
            flash_at = time.time()
        elif ch == ord("a"):
            try:
                runs = terminal.enqueue_cli(
                    list(Test.objects.filter(enabled=True, archived=False)))
                flash = f"queued {len(runs)} run(s)"
            except ValueError as exc:
                flash = str(exc)
            flash_at = time.time()
        elif ch == ord("k"):
            mine = [r for r in act if cur and r.test_id == cur.pk] or act
            if mine:
                terminal.kill_from_terminal(mine[-1].pk)
                flash = f"kill requested for run {mine[-1].pk}"
            else:
                flash = "nothing to kill"
            flash_at = time.time()
        elif ch == ord("w") and cur is not None:
            last = Run.objects.filter(test=cur).order_by("-id").first()
            if last:
                _log_view(stdscr, last)
            else:
                flash, flash_at = "no runs (so no log) yet", time.time()
        elif ch == ord("s") and cur is not None:
            spec = _prompt(stdscr, f'schedule {cur.test_id} ("mon 02:00", '
                                   '"daily 14:30", or "clear"):')
            if spec == "clear":
                terminal.clear_schedules(cur)
                flash = f"{cur.test_id}: schedules cleared"
            elif spec:
                entry = terminal.add_schedule(cur, spec)
                flash = (f"{cur.test_id}: will run {spec} (saved to sidecar)"
                         if entry else f'"{spec}" is not "mon 02:00" shaped')
            else:
                flash = "cancelled"
            flash_at = time.time()
        elif ch == ord("e") and cur is not None:
            flash, flash_at = _run_editor(stdscr, cur), time.time()
        elif ch in (ord("n"), ord("N")):
            tid = _prompt(stdscr, "new test ID (like PAY-001):")
            if tid:
                name = _prompt(stdscr, "readable name:") or tid
                try:
                    filename = terminal.scaffold(tid, name, ts=(ch == ord("N")))
                    flash = f"created {filename} -- press e to edit it"
                except (FileExistsError, Exception) as exc:  # noqa: BLE001
                    flash = f"could not create: {exc}"
            else:
                flash = "cancelled"
            flash_at = time.time()
        elif ch == ord("R"):
            summary = syncer.sync_from_files()
            errs = summary.get("errors") or []
            flash = ("rescanned"
                     + (f" ({len(errs)} problem(s): {errs[0]})" if errs else ""))
            flash_at = time.time()


def run_tui():
    import sys
    if not sys.stdout.isatty():
        print("the TUI needs a real terminal (a TTY). For scripts use:")
        print("  testhub run ID | watch | kill | list | stats | schedule | status")
        raise SystemExit(2)
    curses.wrapper(_main)

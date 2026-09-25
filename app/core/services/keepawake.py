"""Stop the machine sleeping through its own tests.

A box that suspends at 01:50 does not run the 02:00 schedule, and the symptom
is "the scheduler is broken" rather than "the machine went to sleep" -- which
is a genuinely hard afternoon on a machine you cannot easily get to.

So while anything is running, the hub holds a systemd inhibitor lock. It is
reference counted: ten concurrent runs take one lock between them, and it is
released when the last one finishes, so a laptop is still free to sleep when
the hub is idle.

Deliberately NOT held for the lifetime of `serve`: a hub that is up but idle
has no business keeping a machine awake all week. If you do want that, the
right tool is masking the sleep targets once, and `caffeinate --status` says
so.

Everything here is best-effort. Failing to take a lock must never stop a test
from running -- the worst case is the machine sleeps, which is exactly where
we started.
"""
import logging
import subprocess
import threading

log = logging.getLogger("testhub.keepawake")

_lock = threading.Lock()
_holders = 0
_process = None
_unavailable_reason = ""

# Ten years. `sleep infinity` is not reliably available on CentOS 7's
# coreutils, and this process is killed explicitly on release anyway.
_FOREVER = "315360000"


def _start():
    """Take the lock. Returns True if we actually got one."""
    global _process, _unavailable_reason
    try:
        _process = subprocess.Popen(
            ["systemd-inhibit", "--what=idle:sleep", "--who=pw-testhub",
             "--why=running browser tests", "--mode=block", "sleep", _FOREVER],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True)
    except (OSError, ValueError) as exc:
        _unavailable_reason = str(exc)
        _process = None
        return False

    # systemd-inhibit exits immediately when there is no logind to talk to,
    # so "the process started" is not the same as "we hold a lock".
    try:
        _process.wait(timeout=1.0)
        stderr = (_process.stderr.read() or b"").decode("utf-8", "replace")
        _unavailable_reason = stderr.strip().splitlines()[0] if stderr.strip() \
            else "systemd-inhibit exited immediately"
        _process = None
        return False
    except subprocess.TimeoutExpired:
        return True                    # still running == holding the lock


def acquire(why="running tests"):
    """Hold the machine awake. Safe to call from any thread, any number of
    times, as long as each call is matched by release()."""
    global _holders
    with _lock:
        _holders += 1
        if _holders == 1 and _process is None:
            if _start():
                log.info("holding a sleep inhibitor while work is in flight")
            else:
                log.info("no sleep inhibitor available (%s) -- tests still run",
                         _unavailable_reason or "unknown")


def release():
    global _holders, _process
    with _lock:
        _holders = max(0, _holders - 1)
        if _holders == 0 and _process is not None:
            try:
                _process.terminate()
                _process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    _process.kill()
                except OSError:
                    pass
            _process = None
            log.info("released the sleep inhibitor (nothing running)")


def status() -> dict:
    """For the UI and the doctor."""
    with _lock:
        held = _process is not None and _process.poll() is None
        return {"held": held, "holders": _holders,
                "reason": _unavailable_reason if not held else ""}


def can_inhibit() -> bool:
    """Could this machine take a lock at all? Used by diagnostics."""
    try:
        result = subprocess.run(
            ["systemd-inhibit", "--what=idle", "--who=pw-testhub",
             "--why=probe", "--mode=block", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class holding:
    """Context manager: `with keepawake.holding(): ...`"""

    def __init__(self, why="running tests", enabled=True):
        self.why = why
        self.enabled = enabled

    def __enter__(self):
        if self.enabled:
            acquire(self.why)
        return self

    def __exit__(self, *exc):
        if self.enabled:
            release()
        return False

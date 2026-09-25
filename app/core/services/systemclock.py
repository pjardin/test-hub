"""Set the machine's clock from the hub.

Lab machines arrive with clocks that are simply wrong — no NTP reaches an
air-gapped network to fix them — and a wrong clock quietly poisons things
people then debug as hub bugs: schedules fire at surreal hours, run
timestamps sort wrongly, "last run 9 hours ago" for a test that just
finished. Walking a tester through `timedatectl` over the phone is worse
than a button.

The button lives in Settings; the browser supplies ITS time, which on the
tester's laptop is right. Setting the system clock needs root — the hub's
service runs as root, so the normal case just works; anything else gets a
clear sentence, not a traceback.
"""
import os
import re
import shutil
import subprocess
from datetime import datetime

# YYYY-MM-DDTHH:MM[:SS] — exactly what <input type=datetime-local> submits.
# Strict on purpose: this string reaches a system command (always as a list
# argument, never a shell), and a parse here beats a stderr from timedatectl.
_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


class ClockError(Exception):
    """Human-readable; shown to the tester as-is."""


def _run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def set_system_time(value: str) -> str:
    """Set the OS clock to `value` (browser-local wall time). Returns the
    time actually set, formatted for display."""
    if not _FORMAT.match(value or ""):
        raise ClockError("that is not a date and time (expected "
                         "YYYY-MM-DDTHH:MM, from the picker)")
    try:
        when = datetime.strptime(value[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ClockError("that date does not exist — check the day and month")
    seconds = int(value[17:19]) if len(value) >= 19 else 0
    if os.geteuid() != 0:
        raise ClockError("setting the clock needs root. The hub's systemd "
                         "service runs as root and can do it; a hub started "
                         "by hand as a user cannot.")

    stamp = when.strftime("%Y-%m-%d %H:%M:") + f"{seconds:02d}"
    if shutil.which("timedatectl"):
        # NTP being 'active' makes timedatectl refuse a manual time; on an
        # air-gapped box NTP never syncs anyway, so switch it off first.
        _run(["timedatectl", "set-ntp", "false"])
        proc = _run(["timedatectl", "set-time", stamp])
        if proc.returncode != 0:
            raise ClockError("timedatectl refused: "
                             + (proc.stderr or proc.stdout).strip()[:200])
    elif shutil.which("date"):
        proc = _run(["date", "-s", stamp])
        if proc.returncode != 0:
            raise ClockError("date -s refused: "
                             + (proc.stderr or proc.stdout).strip()[:200])
        # keep the hardware clock in agreement so the fix survives a reboot
        if shutil.which("hwclock"):
            _run(["hwclock", "--systohc"])
    else:
        raise ClockError("neither timedatectl nor date exists on this "
                         "machine — set the clock by hand")
    return stamp

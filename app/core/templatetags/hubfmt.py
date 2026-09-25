"""Display helpers shared by the server-rendered pages.

Kept deliberately in step with fmtDur() in static/core/app.js: the same run
must not read "1802.4s" on the runs list and "30m 02s" on the dashboard.
"""
from django import template

register = template.Library()


@register.filter
def duration(seconds):
    """Seconds as a human span: 42s, 3m 05s, 1h 12m.

    Long waits are a supported shape (a 30-minute job is a normal test), and
    "1802.4s" is not something a tester should have to divide by 60.
    """
    if seconds is None or seconds == "":
        return "—"
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "—"
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@register.filter
def at(seconds):
    """A moment in a run, on the activity replay's clock: +0.72s, +1:02.35,
    +1:02:03.50. app.js `fmtAt` prints the same, so a step's time and the
    replay's time can be read against each other."""
    try:
        s = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"+{s:.2f}s"
    if s < 3600:
        minutes, rest = divmod(s, 60)
        return f"+{int(minutes)}:{rest:05.2f}"
    hours, rest = divmod(s, 3600)
    minutes, rest = divmod(rest, 60)
    return f"+{int(hours)}:{int(minutes):02d}:{rest:05.2f}"


@register.filter
def ms_short(ms):
    """A step's duration: 239 ms, 1.44 s, then the usual 3m 05s."""
    try:
        ms = max(0.0, float(ms))
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60000:
        return f"{ms / 1000:.2f} s"
    return duration(ms / 1000)

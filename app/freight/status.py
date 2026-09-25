"""The public status page: what an incident looks like from the outside.

A status page is hosted apart from the site it reports on, so it stays up
when the site does not -- here it is exempt from the incident simulator, like
the Release console. Everything on it comes from the simulated incidents in
the console's history (its last 50 events): the current state, the list of
past incidents, and 30 days of uptime bars. Days before the first recorded
event show "no data" rather than an invented 100%.
"""
import datetime as dt

from freight import releases

COMPONENTS = [
    ("website", "Website", "Home page, tracking and quotes"),
    ("portal", "Staff portal", "Dashboard, shipments, docks and the live fleet"),
    ("api", "Public API", "api/v1 for partner integrations"),
    ("jobs", "Background jobs", "Route optimizer, manifest import, monthly reports"),
    ("mail", "Email", "Password resets and notifications"),
]
STATES = {
    "operational": "Operational",
    "degraded": "Degraded performance",
    "outage": "Major outage",
}
HEADLINES = {
    "operational": "All systems operational",
    "degraded": "Degraded performance: every page is slower than normal",
    "outage": "Major outage: the site is down for maintenance",
}
KIND_STATE = {"outage": "outage", "slow": "degraded"}
DAYS = 30


def _at(entry):
    try:
        when = dt.datetime.fromisoformat(entry["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return when if when.tzinfo else when.astimezone()


def incidents(history, now):
    """Incidents oldest first: {kind, state, start, end (None = ongoing),
    version}. A new incident while one runs ends the first."""
    out, current = [], None
    for entry in sorted((h for h in history if _at(h)), key=_at):
        what, when = entry.get("what"), _at(entry)
        if what in KIND_STATE:
            if current:
                current["end"] = when
                out.append(current)
            current = {"kind": what, "state": KIND_STATE[what], "start": when, "end": None,
                       "version": entry.get("version", "")}
        elif what == "clear" and current:
            current["end"] = when
            out.append(current)
            current = None
    if current:
        out.append(current)
    for inc in out:
        inc["minutes"] = max(1, round(((inc["end"] or now) - inc["start"]).total_seconds() / 60))
    return out


def _overlap(inc, lo, hi, now):
    start, end = max(inc["start"], lo), min(inc["end"] or now, hi)
    return max(0.0, (end - start).total_seconds())


def summary(history=None, incident=None, now=None):
    """Everything the status page shows."""
    now = now or dt.datetime.now().astimezone()
    history = releases.deploy_history() if history is None else history
    incident = releases.incident() if incident is None else incident
    state = KIND_STATE.get(incident, "operational")
    incs = incidents(history, now)
    stamps = [t for t in (_at(h) for h in history) if t]
    first = min(stamps) if stamps else None

    days, observed, down = [], 0.0, 0.0
    for back in range(DAYS - 1, -1, -1):
        day = (now - dt.timedelta(days=back)).date()
        lo = dt.datetime.combine(day, dt.time.min, tzinfo=now.tzinfo)
        hi = min(lo + dt.timedelta(days=1), now)
        if first is None or hi <= first:
            days.append({"date": day, "state": "nodata", "label": f"{day}: no data"})
            continue
        lo = max(lo, first)
        out_s = sum(_overlap(i, lo, hi, now) for i in incs if i["kind"] == "outage")
        slow_s = sum(_overlap(i, lo, hi, now) for i in incs if i["kind"] == "slow")
        observed += (hi - lo).total_seconds()
        down += out_s
        if out_s:
            day_state, label = "outage", f"{day}: down {max(1, round(out_s / 60))} min"
        elif slow_s:
            day_state, label = "degraded", f"{day}: slow {max(1, round(slow_s / 60))} min"
        else:
            day_state, label = "operational", f"{day}: no incidents"
        days.append({"date": day, "state": day_state, "label": label})

    ongoing = incs[-1] if incs and incs[-1]["end"] is None and state != "operational" else None
    return {
        "state": state,
        "headline": HEADLINES[state],
        "since": ongoing["start"] if ongoing else None,
        "components": [{"key": key, "name": name, "what": what, "state": state,
                        "label": STATES[state]} for key, name, what in COMPONENTS],
        "incidents": list(reversed(incs))[:10],
        "days": days,
        "uptime": round(100.0 * (1 - down / observed), 3) if observed else None,
    }

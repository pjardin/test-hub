"""Dock scheduling: today's arrivals at the Oakland depot, onto dock doors.

The staff page is drag-and-drop -- a classic automation headache -- with
rules a real yard has: hazardous goods only at the hazmat door, heavy loads
only where a forklift is, one truck per door per hour. The rules live HERE,
on the server; the page only asks, and a drop the server refuses snaps back
with the reason. The schedule is kept in the visitor's session, so every
test starts with an empty yard.
"""
import random
from collections import namedtuple

from freight import catalog

Arrival = namedtuple("Arrival", "id customer origin weight_kg pieces hazardous heavy eta")

DOORS = [("D1", "Door 1", "forklift"), ("D2", "Door 2", "forklift"),
         ("D3", "Door 3", ""), ("D4", "Door 4", "hazmat")]
SLOTS = ["08:00", "09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]
HEAVY_KG = 1000
KINDS = ["hazardous", "heavy", "normal", "normal", "heavy", "normal",
         "hazardous", "normal"]
SESSION_KEY = "freight_docks"


def arrivals(day=None):
    """The day's eight inbound trucks: two hazardous, two heavy, four normal
    -- generated from the date, so every machine sees the same yard."""
    day = day or catalog.today()
    rng = random.Random(f"docks|{day.isoformat()}")
    origins = catalog.region_stops("california")[:30]
    out = []
    for i, kind in enumerate(KINDS):
        weight = rng.randint(1100, 4200) if kind == "heavy" else rng.randint(60, 900)
        out.append(Arrival(
            id=f"IN-{day:%m%d}-{i + 1:02d}", customer=rng.choice(catalog.CUSTOMERS),
            origin=rng.choice(origins).name, weight_kg=weight,
            pieces=rng.randint(1, 24), hazardous=kind == "hazardous",
            heavy=weight > HEAVY_KG, eta=SLOTS[i]))
    return out


def by_id(day=None):
    return {a.id: a for a in arrivals(day)}


def door_kind(door):
    return {d: kind for d, _, kind in DOORS}.get(door)


def problem(arrival, door):
    """Why this arrival cannot use this door -- or None."""
    kind = door_kind(door)
    if arrival.hazardous and kind != "hazmat":
        return (f"{arrival.id} carries hazardous goods: only Door 4 "
                f"(hazmat certified) may take it")
    if arrival.heavy and kind != "forklift":
        return (f"{arrival.id} weighs {arrival.weight_kg:,} kg: it needs a "
                f"forklift door (Door 1 or 2)")
    return None


def assignments(session):
    return dict((session.get(SESSION_KEY) or {}).get("assignments", {}))


def _save(session, table):
    session[SESSION_KEY] = {"assignments": table}


def assign(session, arrival_id, door, slot):
    """-> error message, or None when the arrival now sits at door/slot."""
    arrival = by_id().get(arrival_id)
    if arrival is None:
        return f"no arrival {arrival_id!r} today"
    if door_kind(door) is None or slot not in SLOTS:
        return f"no dock door {door!r} at {slot!r}"
    reason = problem(arrival, door)
    if reason:
        return reason
    table = assignments(session)
    for other, (d, s) in table.items():
        if other != arrival_id and (d, s) == (door, slot):
            return f"{dict((x, n) for x, n, _ in DOORS)[door]} at {slot} already has {other}"
    table[arrival_id] = [door, slot]
    _save(session, table)
    return None


def unassign(session, arrival_id):
    table = assignments(session)
    table.pop(arrival_id, None)
    _save(session, table)


def clear(session):
    _save(session, {})


def auto(session):
    """Greedy: the hardest loads first (hazmat, then heavy), each into the
    first free door that may take it, from its expected hour on."""
    table = assignments(session)
    taken = {tuple(v) for v in table.values()}
    todo = [a for a in arrivals() if a.id not in table]
    todo.sort(key=lambda a: (not a.hazardous, not a.heavy, a.eta))
    for arrival in todo:
        start = SLOTS.index(arrival.eta)
        for slot in SLOTS[start:] + SLOTS[:start]:
            door = next((d for d, _, _ in DOORS
                         if problem(arrival, d) is None and (d, slot) not in taken), None)
            if door:
                table[arrival.id] = [door, slot]
                taken.add((door, slot))
                break
    _save(session, table)


def board(session):
    """What the page draws: the grid (slot rows x door columns) and the
    arrivals not yet placed."""
    everyone = by_id()
    table = assignments(session)
    cells = {tuple(v): everyone[k] for k, v in table.items() if k in everyone}
    rows = [{"slot": slot,
             "cells": [{"door": d, "key": f"{d}|{slot}", "arrival": cells.get((d, slot))}
                       for d, _, _ in DOORS]}
            for slot in SLOTS]
    waiting = [a for a in everyone.values() if a.id not in table]
    return rows, waiting

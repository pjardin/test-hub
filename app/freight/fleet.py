"""The live fleet: delivery trucks driving their routes in (demo) real time.

Positions are a pure function of the clock -- no state, no background thread:
every process and every browser sees the same truck in the same place, and
a restart changes nothing. Demo time runs fast (a 150 km leg takes about 25
seconds) so a person, or a test, can watch a truck arrive.

Every arrival has a stable id ("CA-3.8841.2" = truck, lap, stop), so a test
can wait for ONE specific arrival rather than any arrival at that city --
the feed also shows the previous lap's arrivals at the same stops.
"""
import math
import random
import time
import zlib

from freight import catalog, geo
from freight.regions import REGIONS

TRUCKS = 6
STOPS_PER_ROUTE = 5
ROAD_FACTOR = 1.25
SPEED_KM_PER_S = 6.0         # demo time: a 150 km leg in 25 s
DWELL_S = 12                 # unloading at a stop (twice that at the depot)
EVENT_WINDOW_S = 600
MAX_EVENTS = 12
PREFIX = {"california": "CA", "northeast": "NE", "texas": "TX", "central_europe": "EU"}
DRIVERS = ["Rosa Delgado", "Marcus Webb", "Aiko Tanaka", "Samir Haddad",
           "Lena Fischer", "Tomás Rivera", "Grace Okafor", "Nils Berg",
           "Priya Raman", "Owen Brooks", "Chiara Russo", "Dmitri Volkov"]
COLORS = ["#e6194b", "#2f9e44", "#4263eb", "#f08c00", "#9c36b5", "#0c8599"]

_routes = {}


def routes(region):
    """The trucks of a region and their fixed loops (depot -> 5 stops -> depot)."""
    if region not in _routes:
        rng = random.Random(f"fleet|{region}")
        depot = catalog.depot(region)
        candidates = catalog.region_stops(region)
        trucks = []
        for i in range(TRUCKS):
            stops = rng.sample(candidates, STOPS_PER_ROUTE)
            # visit in angle order around the depot: a plausible loop, not a zigzag
            stops.sort(key=lambda c: math.atan2(c.lat - depot.lat, c.lon - depot.lon))
            points = [depot] + stops + [depot]
            segments = []           # (kind, seconds, from_index, to_index)
            for k, (a, b) in enumerate(zip(points, points[1:])):
                km = catalog.haversine_km(a, b) * ROAD_FACTOR
                segments.append(("drive", km / SPEED_KM_PER_S, k, k + 1))
                last = k + 1 == len(points) - 1
                segments.append(("dwell", DWELL_S * (2 if last else 1), k + 1, k + 1))
            cycle = sum(seconds for _, seconds, _, _ in segments)
            trucks.append({
                "id": f"{PREFIX[region]}-{i + 1}", "driver": DRIVERS[(i * 5 + len(region)) % len(DRIVERS)],
                "color": COLORS[i % len(COLORS)], "points": points, "segments": segments,
                "cycle": cycle, "offset": rng.uniform(0, cycle), "load": rng.randint(35, 95),
            })
        _routes[region] = trucks
    return _routes[region]


def _late(arrival_id):
    """Deterministic: about one arrival in seven runs late."""
    h = zlib.crc32(arrival_id.encode())
    return (h % 7 == 0), 3 + h % 12


def _arrival_text(truck, point_index, lap):
    place = truck["points"][point_index]
    arrival_id = f"{truck['id']}.{lap}.{point_index}"
    late, minutes = _late(arrival_id)
    if point_index == len(truck["points"]) - 1:
        what = f"back at the {place.name} depot"
    else:
        what = f"arrived at {place.name}"
    return arrival_id, place.name, what, (f"{minutes} min late" if late else "on time")


def state(region, now=None):
    """Where every truck is, and the arrivals of the last ten minutes."""
    if region not in REGIONS:
        raise KeyError(region)
    now = time.time() if now is None else now
    proj = geo.projection(region)
    trucks, events = [], []
    for tr in routes(region):
        clock = now + tr["offset"]
        lap = int(clock // tr["cycle"])
        phase = clock - lap * tr["cycle"]
        elapsed = 0.0
        segs = tr["segments"]
        for n, (kind, seconds, a, b) in enumerate(segs):
            if phase < elapsed + seconds or n == len(segs) - 1:
                into = phase - elapsed
                start, end = tr["points"][a], tr["points"][b]
                if kind == "drive":
                    frac = max(0.0, min(1.0, into / seconds)) if seconds else 1.0
                    lat = start.lat + (end.lat - start.lat) * frac
                    lon = start.lon + (end.lon - start.lon) * frac
                    status, eta, target, target_lap = "driving", seconds - into, b, lap
                else:
                    lat, lon = start.lat, start.lon
                    status = "loading" if a in (0, len(tr["points"]) - 1) else "unloading"
                    # the next arrival: the next drive's end -- next lap if this is the depot
                    if n + 1 < len(segs):
                        nxt = segs[n + 1]
                        target, target_lap = nxt[3], lap
                        eta = (seconds - into) + nxt[1]
                    else:
                        first = segs[0]
                        target, target_lap = first[3], lap + 1
                        eta = (seconds - into) + first[1]
                break
            elapsed += seconds
        x, y = proj.xy(lat, lon)
        arrival_id, place, _, _ = _arrival_text(tr, target, target_lap)
        trucks.append({
            "id": tr["id"], "driver": tr["driver"], "color": tr["color"],
            "status": status, "x": x, "y": y, "load": tr["load"],
            "next_stop": place, "next_arrival": arrival_id, "eta_s": int(math.ceil(eta)),
        })
        # arrivals of this lap and the previous one that fall in the window
        for back in (0, 1):
            k = lap - back
            t = now - phase - back * tr["cycle"]      # start of lap k in wall-clock time
            for kind, seconds, a, b in segs:
                t += seconds
                if kind == "drive" and now - EVENT_WINDOW_S < t <= now:
                    arrival_id, place, what, punctual = _arrival_text(tr, b, k)
                    events.append({"id": arrival_id, "truck": tr["id"], "stop": place,
                                   "at": t, "clock": time.strftime("%H:%M:%S", time.localtime(t)),
                                   "text": f"{tr['id']} {what}", "punctual": punctual})
    events.sort(key=lambda e: e["at"], reverse=True)
    return {"region": region, "now": now, "trucks": trucks, "events": events[:MAX_EVENTS]}


def route_lines(region):
    """Each truck's loop as SVG polyline points, for the map underlay."""
    proj = geo.projection(region)
    return [{"id": tr["id"], "color": tr["color"],
             "points": geo.polyline(proj, [(p.lat, p.lon) for p in tr["points"]]),
             "stops": [{"name": p.name, "x": proj.xy(p.lat, p.lon)[0],
                        "y": proj.xy(p.lat, p.lon)[1]} for p in tr["points"][1:-1]]}
            for tr in routes(region)]

"""Acme Freight's world: cities, air hubs, customers and shipments.

Nothing here is stored. Cities come from the bundled Natural Earth extract
(data/geo.json); shipments are GENERATED from a fixed seed, so every
machine -- a laptop, the lab box, the AWS instance -- shows the same
shipments, and a test written against AF-100001 works everywhere. Dates are
laid out relative to TODAY, so the dashboard always looks like a live
business rather than a museum.

Plain Python, no Django: the route optimizer and the report builder run in
worker threads that must never touch the ORM.
"""
import bisect
import datetime as dt
import difflib
import json
import math
import random
import threading
from collections import namedtuple
from pathlib import Path

from freight.regions import REGION_MIN_POPULATION, REGIONS

# NOT named "data": the app-zip build excludes every directory called data
# (it means "local run data" there), which once shipped the demo without
# its maps. build-app-bundle.sh now fails the build if geo.json is missing.
DATA_DIR = Path(__file__).resolve().parent / "assets"

City = namedtuple("City", "name ascii admin1 iso2 country lat lon pop")

# --------------------------------------------------------------------------
# geography
# --------------------------------------------------------------------------

_geo = None
# re-entrant: cities() holds it while geo() may still need to load the file
_geo_lock = threading.RLock()


def geo():
    """The bundled map + city data, loaded once."""
    global _geo
    if _geo is None:
        with _geo_lock:
            if _geo is None:
                with open(DATA_DIR / "geo.json", encoding="utf-8") as fh:
                    _geo = json.load(fh)
    return _geo


_cities = None
_by_key = None


def cities():
    """Every bundled city, biggest first."""
    global _cities, _by_key
    if _cities is None:
        with _geo_lock:
            if _cities is None:
                rows = [City(*row) for row in geo_rows()]
                index = {}
                for c in rows:
                    for key in _lookup_keys(c):
                        index.setdefault(key, c)   # biggest wins a tie
                _by_key = index
                _cities = rows
    return _cities


def geo_rows():
    return geo()["cities"]


# People write "Fort Worth" and "St. Louis"; the map data says "Ft. Worth"
# and "St. Louis". Both sides are normalised to the long form.
_ALIASES = {"ft": "fort", "st": "saint", "mt": "mount"}


def _norm(text):
    words = str(text or "").lower().replace(".", " ").split()
    return " ".join(_ALIASES.get(w, w) for w in words).replace(" ,", ",")


def _lookup_keys(c):
    for name in {c.name, c.ascii}:
        n = _norm(name)
        yield n
        yield f"{n}, {_norm(c.admin1)}"
        yield f"{n}, {_norm(c.iso2)}"
        yield f"{n}, {_norm(c.country)}"


def label(city):
    """'Oakland, California, US' -- what forms show and accept back."""
    parts = [city.name]
    if city.admin1 and city.admin1 != city.name:
        parts.append(city.admin1)
    parts.append(city.iso2 or city.country)
    return ", ".join(parts)


def find_city(text):
    """Exact lookup by name, 'name, state', 'name, country' or the full
    label a form showed. Ambiguous bare names resolve to the biggest city
    (Portland -> Portland, Oregon), the way a person would read them."""
    cities()
    key = _norm(text)
    if not key:
        return None
    if key in _by_key:
        return _by_key[key]
    # the full 'name, admin1, CC' label: try name + last part, then name + admin1
    parts = [p.strip() for p in key.split(",") if p.strip()]
    if len(parts) >= 2:
        for tail in (parts[-1], parts[1]):
            hit = _by_key.get(f"{parts[0]}, {tail}")
            if hit:
                return hit
    return None


def suggest(text, limit=8):
    """Autocomplete: prefix matches first (biggest first), then fuzzy."""
    key = _norm(text)
    if len(key) < 2:
        return []
    out = []
    for c in cities():
        if _norm(c.name).startswith(key) or _norm(c.ascii).startswith(key):
            out.append(c)
            if len(out) >= limit:
                return out
    if len(out) < limit:
        names = {}
        for c in cities():
            names.setdefault(_norm(c.ascii), c)
        for n in difflib.get_close_matches(key, list(names), n=limit, cutoff=0.75):
            if names[n] not in out:
                out.append(names[n])
    return out[:limit]


def did_you_mean(text):
    hits = suggest(text, limit=1)
    return label(hits[0]) if hits else ""


def haversine_km(a, b):
    """Great-circle distance between two cities (or (lat, lon) pairs)."""
    lat1, lon1 = (a.lat, a.lon) if isinstance(a, City) else a
    lat2, lon2 = (b.lat, b.lon) if isinstance(b, City) else b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


# --------------------------------------------------------------------------
# regions (ground delivery) and hubs (the air network)
# --------------------------------------------------------------------------

def in_region(city, key):
    region = REGIONS[key]
    x0, y0, x1, y1 = region["bbox"]
    if not (x0 <= city.lon <= x1 and y0 <= city.lat <= y1):
        return False
    return not region["countries"] or city.iso2 in region["countries"]


def depot(key):
    name, iso = REGIONS[key]["depot"]
    return find_city(f"{name}, {iso}")


def region_stops(key):
    """Delivery-stop candidates: every sizeable city in the region, minus
    the depot, biggest first."""
    home = depot(key)
    return [c for c in cities()
            if c is not home and c.pop >= REGION_MIN_POPULATION and in_region(c, key)]


# (code, city, ISO country, first release that serves it)
HUBS = [
    ("LAX", "Los Angeles", "US", "1.0.0"), ("ORD", "Chicago", "US", "1.0.0"),
    ("JFK", "New York", "US", "1.0.0"), ("ATL", "Atlanta", "US", "1.0.0"),
    ("DFW", "Dallas", "US", "1.0.0"), ("YYZ", "Toronto", "CA", "1.0.0"),
    ("MEX", "Mexico City", "MX", "1.0.0"), ("GRU", "São Paulo", "BR", "1.0.0"),
    ("LHR", "London", "GB", "1.0.0"), ("CDG", "Paris", "FR", "1.0.0"),
    ("FRA", "Frankfurt", "DE", "1.0.0"), ("MAD", "Madrid", "ES", "1.0.0"),
    ("DXB", "Dubai", "AE", "1.0.0"), ("BOM", "Mumbai", "IN", "1.0.0"),
    ("SIN", "Singapore", "SG", "1.0.0"), ("HKG", "Hong Kong", "HK", "1.0.0"),
    ("PVG", "Shanghai", "CN", "1.0.0"), ("NRT", "Tokyo", "JP", "1.0.0"),
    ("SYD", "Sydney", "AU", "1.0.0"),
    # the 2.0 "new markets" expansion -- visible on the home page map
    ("JNB", "Johannesburg", "ZA", "2.0.0"), ("LOS", "Lagos", "NG", "2.0.0"),
    ("IST", "Istanbul", "TR", "2.0.0"), ("ICN", "Seoul", "KR", "2.0.0"),
]

LANES = [
    ("LAX", "NRT"), ("LAX", "SYD"), ("LAX", "HKG"), ("LAX", "ORD"), ("LAX", "MEX"),
    ("ORD", "JFK"), ("ORD", "FRA"), ("ORD", "DFW"), ("ORD", "YYZ"), ("ATL", "JFK"),
    ("ATL", "GRU"), ("ATL", "DFW"), ("DFW", "MEX"), ("JFK", "LHR"), ("JFK", "CDG"),
    ("JFK", "FRA"), ("JFK", "GRU"), ("LHR", "CDG"), ("LHR", "DXB"), ("LHR", "MAD"),
    ("FRA", "DXB"), ("FRA", "PVG"), ("CDG", "MAD"), ("MAD", "GRU"), ("DXB", "BOM"),
    ("DXB", "SIN"), ("BOM", "SIN"), ("SIN", "HKG"), ("SIN", "SYD"), ("HKG", "PVG"),
    ("PVG", "NRT"), ("HKG", "NRT"),
    ("FRA", "JNB"), ("DXB", "JNB"), ("LHR", "LOS"), ("JNB", "LOS"), ("FRA", "IST"),
    ("IST", "DXB"), ("ICN", "NRT"), ("ICN", "PVG"), ("ICN", "LAX"),
]

Hub = namedtuple("Hub", "code city since")
FIRST_NETWORK = "1.0.0"


def _vkey(version):
    return tuple(int(p) for p in version.split("."))


_hub_cache = {}


def hubs(version="99.0.0"):
    """The air hubs a release serves (2.0 opened new markets)."""
    if version not in _hub_cache:
        out = []
        for code, name, iso, since in HUBS:
            city = find_city(f"{name}, {iso}")
            if city and _vkey(since) <= _vkey(version):
                out.append(Hub(code, city, since))
        _hub_cache[version] = out
    return _hub_cache[version]


def lanes(version="99.0.0"):
    served = {h.code: h for h in hubs(version)}
    return [(served[a], served[b]) for a, b in LANES if a in served and b in served]


def nearest_hub(city, version="99.0.0"):
    return min(hubs(version), key=lambda h: haversine_km(city, h.city))


# --------------------------------------------------------------------------
# customers and shipments (generated, deterministic)
# --------------------------------------------------------------------------

CUSTOMERS = [
    "Brightwater Foods", "Cedar & Pine Furniture", "Lumen Optics", "Orbit Bicycles",
    "Harborview Medical Supply", "Juniper Textiles", "Copperline Electric",
    "Blue Mesa Coffee", "Stonebridge Tools", "Northfield Seeds", "Kitehill Toys",
    "Silverleaf Pharma", "Redwood Paper Co.", "Summit Outdoor Gear", "Tidal Audio",
    "Granite State Ceramics", "Wildflower Cosmetics", "Ironclad Fasteners",
    "Meadowlark Dairy", "Polar Star Refrigeration", "Sunstone Solar",
    "Quarry Lane Books", "Maple & Main Hardware", "Beacon Marine Parts",
    "Hollow Oak Instruments", "Riverbend Apparel", "Skyline Drones",
    "Crescent Bakeries", "Evergreen Labs", "Foxglove Botanicals",
    "Glacier Water Systems", "Keystone Robotics", "Lighthouse Printing",
    "Mosaic Tile Works", "Nimbus Cloud Hardware", "Oakmont Precision",
    "Prairie Wind Energy", "Quartzite Semiconductors", "Saltmarsh Seafood",
    "Thistle & Rose Florists",
]

SERVICES = ("economy", "standard", "express")
STATUSES = ("booked", "picked_up", "in_transit", "at_hub", "out_for_delivery",
            "delivered", "exception")
STATUS_LABELS = {
    "booked": "Booked", "picked_up": "Picked up", "in_transit": "In transit",
    "at_hub": "At hub", "out_for_delivery": "Out for delivery",
    "delivered": "Delivered", "exception": "Exception",
}
TRANSIT_DAYS = {"economy": 6, "standard": 4, "express": 2}

Shipment = namedtuple(
    "Shipment",
    "id customer origin destination origin_hub dest_hub service weight_kg "
    "pieces created eta status delivered on_time events price")

SEED = 20260301
COUNT = 640


def business_days(start, days):
    d = start
    while days > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def _events(rng, origin, dest, ohub, dhub, created, status, service):
    """A believable scan history up to the shipment's current status."""
    steps = [("booked", created, f"Booked with Acme Freight ({service})", origin)]
    t = created + dt.timedelta(hours=rng.randint(3, 20))
    steps.append(("picked_up", t, "Picked up by courier", origin))
    t += dt.timedelta(hours=rng.randint(4, 16))
    steps.append(("at_hub", t, f"Arrived at {ohub.code} hub", ohub.city))
    t += dt.timedelta(hours=rng.randint(6, 30))
    steps.append(("in_transit", t, f"Departed {ohub.code} for {dhub.code}", ohub.city))
    t += dt.timedelta(hours=rng.randint(8, 40))
    steps.append(("at_hub", t, f"Arrived at {dhub.code} hub", dhub.city))
    t += dt.timedelta(hours=rng.randint(6, 26))
    steps.append(("out_for_delivery", t, "Out for delivery", dest))
    t += dt.timedelta(hours=rng.randint(2, 9))
    steps.append(("delivered", t, "Delivered -- signed for at reception", dest))
    order = {"booked": 1, "picked_up": 2, "at_hub": 3, "in_transit": 4,
             "out_for_delivery": 6, "delivered": 7, "exception": 4}
    keep = steps[:order[status]]
    if status == "at_hub" and rng.random() < 0.5:
        keep = steps[:5]
    if status == "exception":
        reason = rng.choice(["Customs hold -- documents requested",
                             "Address could not be verified",
                             "Weather delay at hub", "Damaged packaging -- inspecting"])
        keep.append(("exception", keep[-1][1] + dt.timedelta(hours=rng.randint(2, 12)),
                     reason, keep[-1][3]))
    return keep


def _fit_before(events, created, now):
    """Scan times are drawn at random; squeeze them so the last one is in
    the past. A 'delivered' shipment with a delivery time tomorrow would be
    the first thing a sharp-eyed tester reports."""
    last = events[-1][1]
    limit = now - dt.timedelta(minutes=45)
    if last <= limit or last <= created:
        return events
    factor = max(0.0, (limit - created).total_seconds()) / (last - created).total_seconds()
    return [(code, created + (when - created) * factor, text, where)
            for code, when, text, where in events]


# Hand-placed shipments the sample tests and the tour rely on. Their ids and
# statuses never change; everything else is generated around them.
FIXTURES = {
    "AF-100001": ("Lumen Optics", ("Los Angeles", "US"), ("Frankfurt", "DE"),
                  "express", 42.5, 3, "in_transit"),
    "AF-100002": ("Blue Mesa Coffee", ("Seattle", "US"), ("Chicago", "US"),
                  "standard", 310.0, 12, "delivered"),
    "AF-100003": ("Silverleaf Pharma", ("Mumbai", "IN"), ("Boston", "US"),
                  "express", 18.2, 2, "exception"),
    "AF-100004": ("Orbit Bicycles", ("Toronto", "CA"), ("Mexico City", "MX"),
                  "economy", 780.0, 20, "out_for_delivery"),
    "AF-100005": ("Kitehill Toys", ("Shanghai", "CN"), ("San Francisco", "US"),
                  "standard", 1250.0, 48, "at_hub"),
}

_ship_lock = threading.Lock()
_ship_cache = {}


def today():
    return dt.date.today()


# The shipment history is a snapshot of the network at 09:00 -- the morning's
# scans are in -- and the snapshot a machine shows is the LATEST 09:00 that
# has already happened: before 9 a.m. that is yesterday's. A calendar-day
# snapshot showed, to anyone looking before 8:15, scans stamped later that
# same morning.
SNAPSHOT_HOUR = 9


def snapshot_day(now=None):
    """The day of the newest 09:00 snapshot at `now` (default: this moment)."""
    now = now or dt.datetime.now()
    return (now - dt.timedelta(hours=SNAPSHOT_HOUR)).date()


def shipments():
    """Every shipment, newest first. Regenerated once a day, at the 09:00
    snapshot, so 'last 30 days' always means the last 30 days."""
    day = snapshot_day()
    with _ship_lock:
        if day not in _ship_cache:
            _ship_cache.clear()
            _ship_cache[day] = _generate(day)
        return _ship_cache[day]


def _generate(day):
    from freight import pricing   # pricing imports catalog; avoid a cycle
    rng = random.Random(SEED)
    now = dt.datetime.combine(day, dt.time(SNAPSHOT_HOUR, 0))
    pool = [c for c in cities() if c.pop >= 400000]
    weights = [math.log(c.pop) for c in pool]
    cumulative = []
    total = 0.0
    for w in weights:
        total += w
        cumulative.append(total)

    def pick_city():
        return pool[bisect.bisect_left(cumulative, rng.random() * total)]

    out = []
    for n in range(COUNT):
        sid = f"AF-{100001 + n}"
        if sid in FIXTURES:
            cust, o, d, service, kg, pieces, status = FIXTURES[sid]
            origin, dest = find_city(f"{o[0]}, {o[1]}"), find_city(f"{d[0]}, {d[1]}")
            age = {"in_transit": 2, "delivered": 9, "exception": 3,
                   "out_for_delivery": 5, "at_hub": 3}[status]
            created = now - dt.timedelta(days=age, hours=3)
        else:
            cust = rng.choice(CUSTOMERS)
            origin = pick_city()
            dest = pick_city()
            while dest is origin:
                dest = pick_city()
            service = rng.choices(SERVICES, weights=(3, 5, 2))[0]
            kg = round(rng.lognormvariate(4.2, 1.1), 1)
            pieces = max(1, int(kg // rng.randint(8, 40)))
            created = now - dt.timedelta(days=rng.uniform(0, 44))
            age_days = (now - created).total_seconds() / 86400
            expected = TRANSIT_DAYS[service]
            if age_days > expected + 1.5:
                status = "delivered" if rng.random() > 0.05 else "exception"
            else:
                status = rng.choices(
                    ("booked", "picked_up", "at_hub", "in_transit",
                     "out_for_delivery", "exception"),
                    weights=(2, 2, 3, 5, 2, 1))[0]
        # history was made on the ORIGINAL network: the hubs 2.0 opens must
        # not appear in shipments that "happened" before it
        ohub, dhub = nearest_hub(origin, FIRST_NETWORK), nearest_hub(dest, FIRST_NETWORK)
        if dhub is ohub:
            dhub = sorted(hubs(FIRST_NETWORK), key=lambda h: haversine_km(dest, h.city))[1]
        events = _fit_before(
            _events(rng, origin, dest, ohub, dhub, created, status, service),
            created, now)
        eta = business_days(created.date(), TRANSIT_DAYS[service])
        delivered = events[-1][1] if status == "delivered" else None
        on_time = (delivered.date() <= eta) if delivered else None
        price = pricing.quote(origin, dest, kg, service).total
        out.append(Shipment(sid, cust, origin, dest, ohub, dhub, service, kg,
                            pieces, created, eta, status, delivered, on_time,
                            events, price))
    out.sort(key=lambda s: s.created, reverse=True)
    return out


def shipment(sid):
    sid = str(sid or "").strip().upper()
    for s in shipments():
        if s.id == sid:
            return s
    return None

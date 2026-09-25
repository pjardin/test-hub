"""Acme Freight's public REST API, v1 -- what a customer's own system calls.

JSON in, JSON out; an API key in the X-API-Key header; a per-key
requests-per-minute limit that answers 429 with Retry-After. Documented, with
a live example, on /demo/freight/developers/.

It exists so the hub can show API TESTING beside browser testing -- and its
CONTRACT changes by release the way real APIs break: 2.0.0 renames `eta` to
`estimated_delivery` with no version bump and no changelog entry, 2.1.0 also
loses the rate limiter, 3.0.0 sends both names and marks `eta` deprecated.
Money is always a STRING of exact decimals ("117.62"): a JSON float would
turn $0.10 + $0.20 into 0.30000000000000004 in somebody's client.
"""
import collections
import datetime as dt
import functools
import json
import threading
import time
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from freight import catalog, pricing
from freight.web import db, freight_view, release_of

# Demo credentials, published on the developers page on purpose.
API_KEYS = {
    "acme-demo-7f3a91c2": {"name": "Demo partner", "per_minute": 120},
    "acme-trial-2b91e4d0": {"name": "Trial account", "per_minute": 5},
}
MAX_LIST = 50

# What the API changelog says, by the release that introduced it. 2.0.0's
# rename is deliberately NOT here: the undocumented breaking change is the
# point. 3.0.0's entry is the apology.
CHANGELOG = [
    ("1.0.0", "API v1 launched: shipments, shipment lookup and quotes."),
    ("1.1.0", "Shipment lists answer up to 3x faster."),
    ("3.0.0", "Restored 'eta' (2.0.0 had renamed it to 'estimated_delivery' "
              "without notice). Both are sent; 'eta' is deprecated."),
]


class RateLimiter:
    """A sliding one-minute window per key, in the serving process."""

    def __init__(self):
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def check(self, key, per_minute, now=None):
        """0 if the call may go ahead (and it is counted), else the seconds
        until it would."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= per_minute:
                return max(1, int(60 - (now - hits[0])) + 1)
            hits.append(now)
            return 0

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def _error(status, message, **extra):
    body = {"error": message}
    body.update(extra)
    return JsonResponse(body, status=status)


def api_endpoint(view):
    """API key check and rate limit around a v1 endpoint."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        key = request.headers.get("X-API-Key", "").strip()
        account = API_KEYS.get(key)
        if account is None:
            return _error(401, "missing or unknown API key -- send one in the "
                               "X-API-Key header (see /demo/freight/developers/)")
        if release_of(request).flags["api_rate_limit"]:
            wait = limiter.check(key, account["per_minute"])
            if wait:
                response = _error(429, f"rate limit: {account['per_minute']} requests "
                                       f"per minute for this key", retry_after=wait)
                response["Retry-After"] = str(wait)
                return response
        response = view(request, *args, **kwargs)
        if release_of(request).flags["api_rate_limit"]:
            response["X-RateLimit-Limit"] = str(account["per_minute"])
        return response
    return wrapper


def _iso(value, spec="minutes"):
    return value.isoformat(timespec=spec) if value else None


def shipment_json(s, release):
    body = {
        "id": s.id,
        "status": s.status,
        "status_label": catalog.STATUS_LABELS[s.status],
        "customer": s.customer,
        "origin": catalog.label(s.origin),
        "destination": catalog.label(s.destination),
        "service": s.service,
        "weight_kg": s.weight_kg,
        "pieces": s.pieces,
        "booked_at": _iso(s.created),
        "delivered_at": _iso(s.delivered),
        "price": {"amount": str(s.price), "currency": "USD"},
        "events": [{"at": _iso(when), "code": code, "text": text,
                    "location": catalog.label(place)}
                   for code, when, text, place in s.events],
    }
    mode = release.flags["api_eta_field"]
    if mode in ("eta", "both"):
        body["eta"] = s.eta.isoformat()
    if mode in ("estimated_delivery", "both"):
        body["estimated_delivery"] = s.eta.isoformat()
    if mode == "both":
        body["deprecated"] = {"eta": "use estimated_delivery; eta goes away in API v2"}
    return body


@csrf_exempt
@freight_view
def status(request):
    """Public (no key): is the API up, and which release answers."""
    return JsonResponse({"service": "acme-freight", "api": "v1",
                         "release": release_of(request).version,
                         "time": dt.datetime.now().astimezone().isoformat(timespec="seconds")})


@csrf_exempt
@freight_view
@api_endpoint
def shipments(request):
    if request.method != "GET":
        return _error(405, "use GET")
    g = request.GET
    wanted = g.get("status", "")
    if wanted and wanted not in catalog.STATUSES:
        return _error(400, f"unknown status {wanted!r}", field="status",
                      allowed=list(catalog.STATUSES))
    try:
        limit = int(g.get("limit", 20))
        offset = int(g.get("offset", 0))
        if not (1 <= limit <= MAX_LIST) or offset < 0:
            raise ValueError
    except ValueError:
        return _error(400, f"limit is 1..{MAX_LIST} and offset is 0 or more", field="limit")
    customer = g.get("customer", "").strip().lower()
    db.query(request, 2)
    rows = [s for s in catalog.shipments()
            if (not wanted or s.status == wanted)
            and (not customer or customer in s.customer.lower())]
    release = release_of(request)
    return JsonResponse({"count": len(rows), "limit": limit, "offset": offset,
                         "results": [shipment_json(s, release)
                                     for s in rows[offset:offset + limit]]})


@csrf_exempt
@freight_view
@api_endpoint
def shipment(request, sid):
    if request.method != "GET":
        return _error(405, "use GET")
    db.query(request, 2)
    found = catalog.shipment(sid)
    if found is None:
        return _error(404, f"no shipment {sid}")
    return JsonResponse(shipment_json(found, release_of(request)))


@csrf_exempt
@freight_view
@api_endpoint
def quotes(request):
    if request.method != "POST":
        return _error(405, "use POST with a JSON body")
    try:
        body = json.loads(request.body or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return _error(400, "the body must be a JSON object")
    places = {}
    for field in ("origin", "destination"):
        text = str(body.get(field) or "")
        city = catalog.find_city(text) if text else None
        if city is None:
            hint = catalog.did_you_mean(text) if text else ""
            return _error(400, f"{field}: unknown city {text!r}", field=field,
                          did_you_mean=hint or None)
        places[field] = city
    try:
        weight = Decimal(str(body.get("weight_kg", "")))
        if not (0 < weight <= pricing.MAX_KG):
            raise InvalidOperation
    except InvalidOperation:
        return _error(400, f"weight_kg must be a number from 0.1 to {pricing.MAX_KG}",
                      field="weight_kg")
    service = str(body.get("service") or "standard").lower()
    if service not in pricing.SERVICE_INFO:
        return _error(400, "service is economy, standard or express", field="service")
    hazardous = bool(body.get("hazardous"))
    if hazardous and service == "express":
        return _error(400, "hazardous goods cannot fly express", field="service")
    db.query(request, 1)
    q = pricing.quote(places["origin"], places["destination"], weight, service,
                      hazardous=hazardous, promo=str(body.get("promo") or ""),
                      promo_multiplier=release_of(request).flags["promo_multiplier"])
    return JsonResponse({
        "origin": catalog.label(places["origin"]),
        "destination": catalog.label(places["destination"]),
        "service": service, "distance_km": q.distance_km,
        "chargeable_kg": str(q.chargeable_kg),
        "lines": [{"code": line.code, "label": line.label, "amount": str(line.amount)}
                  for line in q.lines],
        "subtotal": str(q.subtotal), "discount": str(q.discount),
        "promo": q.promo or None, "fuel_surcharge": str(q.fuel),
        "total": str(q.total), "currency": "USD",
    })

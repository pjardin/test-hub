"""Acme Freight -- the built-in demo site's pages.

A realistic little business app for the Test Hub to test: a public side
(tracking, a multi-step quote wizard, bookings) and a staff portal behind a
sign-in (dashboard, shipment search, a route optimizer, manifest import and
monthly reports -- the last three are slow server-side jobs with live
progress). Every page renders on the server; the little JavaScript there is
progressive (autocomplete, polling, charts, dialogs).

Which RELEASE is deployed changes speed, look, headers and bugs; see
freight/releases.py. The views stay release-agnostic by reading flags.
"""
import datetime as dt
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.safestring import mark_safe

from core import appconfig
from freight import (accounts, catalog, docks, fleet, geo, jobs, mail, manifest, optimizer,
                     pricing, public_api, releases, reports)
from freight import label as shiplabel
from freight import status as statuspage
from freight.regions import REGIONS
from freight.web import (USERS, booking, current_user, db, flash, freight_view,
                         incident_exempt, login_required, no_store, page, release_of,
                         safe_next, save_booking)

STATUS_ORDER = {s: i for i, s in enumerate(catalog.STATUSES)}
PAGE_SIZE = 25


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _basemap_url(key, release):
    return f"{reverse('freight:basemap', args=[key])}?theme={release.flags['theme']}"


def _changes(request):
    return request.session.get("freight_changes", {})


def _status_of(request, s):
    return _changes(request).get(s.id, {}).get("status", s.status)


def _pin(proj, place, **extra):
    """A labelled map point: position plus the label's baseline above it."""
    x, y = proj.xy(place.lat, place.lon)
    return dict(extra, x=x, y=y, ly=round(y - 8, 1))


def _network(release):
    proj = geo.projection("world")
    hubs = catalog.hubs(release.version)
    return {
        "w": proj.width, "h": proj.height,
        "basemap": _basemap_url("world", release),
        "hubs": [_pin(proj, h.city, code=h.code, city=h.city.name,
                      new=h.since != "1.0.0") for h in hubs],
        "new_hubs": [h.city.name for h in hubs if h.since != "1.0.0"],
        "lanes": [{"d": geo.arc_d(proj, (a.city.lat, a.city.lon), (b.city.lat, b.city.lon)),
                   "name": f"{a.code}-{b.code}"} for a, b in catalog.lanes(release.version)],
    }


def _shipment_ctx(request, s):
    release = release_of(request)
    change = _changes(request).get(s.id, {})
    status = change.get("status", s.status)
    events = [{"code": c, "when": w, "text": t, "where": catalog.label(p)}
              for c, w, t, p in s.events]
    for extra in change.get("events", []):
        events.append({"code": extra["code"],
                       "when": dt.datetime.fromisoformat(extra["when"]),
                       "text": extra["text"], "where": extra["where"]})
    events.sort(key=lambda e: e["when"], reverse=True)
    proj = geo.projection("world")
    stops = [s.origin, s.origin_hub.city, s.dest_hub.city, s.destination]
    legs_done = {"booked": 0, "picked_up": 0, "at_hub": 1, "in_transit": 2,
                 "out_for_delivery": 3, "delivered": 3, "exception": 1}.get(status, 0)
    legs = []
    for i, (a, b) in enumerate(zip(stops, stops[1:])):
        legs.append({"d": geo.arc_d(proj, (a.lat, a.lon), (b.lat, b.lon), steps=32),
                     "done": i < legs_done})
    return {
        "id": s.id, "customer": s.customer,
        "origin": catalog.label(s.origin), "destination": catalog.label(s.destination),
        "origin_hub": s.origin_hub.code, "dest_hub": s.dest_hub.code,
        "service": pricing.SERVICE_INFO[s.service]["label"],
        "weight_kg": s.weight_kg, "pieces": s.pieces,
        "status": status, "status_label": catalog.STATUS_LABELS[status],
        "created": s.created, "eta": s.eta, "delivered": s.delivered,
        "on_time": s.on_time, "price": pricing.fmt(s.price),
        "events": events, "notes": change.get("notes", []),
        "map": {"w": proj.width, "h": proj.height,
                "basemap": _basemap_url("world", release), "legs": legs,
                "points": [_pin(proj, p, name=p.name, kind=k)
                           for p, k in zip(stops, ("origin", "hub", "hub", "destination"))]},
    }


def _int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# public side
# --------------------------------------------------------------------------

@freight_view
def home(request):
    release = release_of(request)
    return page(request, "freight/home.html", {
        "network": _network(release),
        "services": [dict(info, key=k) for k, info in pricing.SERVICE_INFO.items()],
        "stats": {"hubs": len(catalog.hubs(release.version)),
                  "lanes": len(catalog.lanes(release.version)),
                  "shipments": len(catalog.shipments())},
        "examples": list(catalog.FIXTURES)[:3],
    })


@freight_view
def track(request):
    query = (request.GET.get("id") or "").strip().upper()
    ctx = {"query": query}
    if query:
        db.query(request, 3)            # shipment, scan events, hub details
        s = catalog.shipment(query)
        if s is not None:
            ctx["shipment"] = _shipment_ctx(request, s)
        elif booking(query):
            ctx["booking"] = booking(query)
        else:
            ctx["not_found"] = True
    return page(request, "freight/track.html", ctx)


# --- quote wizard ------------------------------------------------------------

def _quote_state(request):
    return dict(request.session.get("freight_quote") or {})


def _save_quote(request, q):
    request.session["freight_quote"] = q


def _steps(q, release):
    steps = [("route", "Route"), ("cargo", "Cargo")]
    if release.flags["customs_step"] and q.get("international"):
        steps.append(("customs", "Customs"))
    steps += [("service", "Service"), ("review", "Review")]
    return steps


def _next_step(q, release, after):
    keys = [k for k, _ in _steps(q, release)]
    return f"freight:quote_{keys[keys.index(after) + 1]}"


def _first_missing(q, release):
    """The earliest step whose data is not there yet -- deep links and the
    back button can never land a visitor on a step it cannot render."""
    if not q.get("origin"):
        return "route"
    if not q.get("weight_kg"):
        return "cargo"
    if (release.flags["customs_step"] and q.get("international")
            and not q.get("customs")):
        return "customs"
    if not q.get("service"):
        return "service"
    return None


def _wizard(request, step, template, extra):
    release = release_of(request)
    q = _quote_state(request)
    ctx = {"q": q, "steps": _steps(q, release), "step": step}
    ctx.update(extra)
    return page(request, template, ctx, status=ctx.pop("status", 200))


def _priced(q, release, service=None, with_promo=True):
    customs = q.get("customs") if (release.flags["customs_step"]
                                   and q.get("international")) else None
    dims = q.get("dims") or None
    return pricing.quote(
        catalog.find_city(q["origin"]), catalog.find_city(q["destination"]),
        q["weight_kg"], service or q.get("service") or "standard",
        dims_cm=dims, hazardous=q.get("hazardous", False), customs=customs,
        promo=q.get("promo", "") if with_promo else "",
        promo_multiplier=release.flags["promo_multiplier"])


@freight_view
def quote_start(request):
    if request.GET.get("restart"):
        request.session.pop("freight_quote", None)
    return redirect("freight:quote_route")


@freight_view
def quote_route(request):
    release = release_of(request)
    q = _quote_state(request)
    values = {"origin": q.get("origin", ""), "destination": q.get("destination", "")}
    errors = {}
    if request.method == "POST":
        values = {k: (request.POST.get(k) or "").strip() for k in values}
        found = {}
        for field, text in values.items():
            city = catalog.find_city(text) if text else None
            if not text:
                errors[field] = "Required"
            elif city is None:
                hint = catalog.did_you_mean(text)
                errors[field] = (f"We do not serve '{text}'"
                                 + (f" -- did you mean {hint}?" if hint else "."))
            else:
                found[field] = city
        if not errors and found["origin"] == found["destination"]:
            errors["destination"] = "Origin and destination are the same city"
        if not errors:
            new_q = {"origin": catalog.label(found["origin"]),
                     "destination": catalog.label(found["destination"]),
                     "international": pricing.is_international(found["origin"],
                                                                found["destination"])}
            # keep later answers only if the route did not change
            if (q.get("origin"), q.get("destination")) == (new_q["origin"], new_q["destination"]):
                q.update(new_q)
            else:
                q = new_q
            _save_quote(request, q)
            return redirect(_next_step(q, release, "route"))
    return _wizard(request, "route", "freight/quote_route.html",
                   {"values": values, "errors": errors,
                    "status": 400 if errors else 200})


@freight_view
def quote_cargo(request):
    release = release_of(request)
    q = _quote_state(request)
    missing = _first_missing(q, release)
    if missing == "route":
        return redirect("freight:quote_route")
    dims = q.get("dims") or ["", "", ""]
    values = {"weight_kg": q.get("weight_kg", ""), "pieces": q.get("pieces", 1),
              "length_cm": dims[0], "width_cm": dims[1], "height_cm": dims[2],
              "hazardous": q.get("hazardous", False),
              "description": q.get("description", "")}
    errors = {}
    if request.method == "POST":
        p = request.POST
        values = {k: (p.get(k) or "").strip() for k in values if k != "hazardous"}
        values["hazardous"] = bool(p.get("hazardous"))
        try:
            weight = Decimal(values["weight_kg"])
            if not (0 < weight <= pricing.MAX_KG):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors["weight_kg"] = f"Enter a weight between 0.1 and {pricing.MAX_KG:,} kg"
        try:
            pieces = int(values["pieces"] or "1")
            if not 1 <= pieces <= 999:
                raise ValueError
        except ValueError:
            errors["pieces"] = "Enter a whole number of pieces, 1 to 999"
        dim_values = [values["length_cm"], values["width_cm"], values["height_cm"]]
        dims_cm = None
        if any(dim_values):
            try:
                dims_cm = [int(v) for v in dim_values]
                if not all(1 <= v <= 400 for v in dims_cm):
                    raise ValueError
            except ValueError:
                errors["dims"] = "Dimensions are whole centimetres, 1 to 400 -- all three, or none"
        if len(values["description"]) > 200:
            errors["description"] = "Keep the description under 200 characters"
        if not errors:
            q.update({"weight_kg": str(weight), "pieces": pieces, "dims": dims_cm,
                      "hazardous": values["hazardous"],
                      "description": values["description"]})
            if values["hazardous"] and q.get("service") == "express":
                q.pop("service", None)
            _save_quote(request, q)
            return redirect(_next_step(q, release, "cargo"))
    return _wizard(request, "cargo", "freight/quote_cargo.html",
                   {"values": values, "errors": errors,
                    "status": 400 if errors else 200})


@freight_view
def quote_customs(request):
    release = release_of(request)
    q = _quote_state(request)
    if not (release.flags["customs_step"] and q.get("international")):
        return redirect("freight:quote_service")
    missing = _first_missing(q, release)
    if missing in ("route", "cargo"):
        return redirect(f"freight:quote_{missing}")
    customs = q.get("customs") or {}
    values = {"category": customs.get("category", "other"),
              "declared_value": customs.get("declared_value", ""),
              "hs_code": customs.get("hs_code", ""),
              "incoterm": customs.get("incoterm", "DAP")}
    errors = {}
    if request.method == "POST":
        values = {k: (request.POST.get(k) or "").strip() for k in values}
        if values["category"] not in pricing.DUTY_RATES:
            errors["category"] = "Choose what is being shipped"
        try:
            declared = Decimal(values["declared_value"])
            if not (0 < declared <= Decimal("5000000")):
                raise InvalidOperation
        except InvalidOperation:
            errors["declared_value"] = "Enter the declared value in US dollars"
        if values["hs_code"] and not all(ch.isdigit() or ch == "." for ch in values["hs_code"]):
            errors["hs_code"] = "An HS code is digits and dots, e.g. 8471.30"
        if values["incoterm"] not in ("DAP", "DDP"):
            errors["incoterm"] = "Choose who pays the import duty"
        if not errors:
            q["customs"] = {"category": values["category"],
                            "declared_value": str(declared),
                            "hs_code": values["hs_code"], "incoterm": values["incoterm"]}
            _save_quote(request, q)
            return redirect(_next_step(q, release, "customs"))
    return _wizard(request, "customs", "freight/quote_customs.html",
                   {"values": values, "errors": errors,
                    "categories": list(pricing.DUTY_RATES),
                    "status": 400 if errors else 200})


@freight_view
def quote_service(request):
    release = release_of(request)
    q = _quote_state(request)
    missing = _first_missing(q, release)
    if missing in ("route", "cargo", "customs"):
        return redirect(f"freight:quote_{missing}")
    error = ""
    if request.method == "POST":
        choice = request.POST.get("service", "")
        if choice not in pricing.SERVICE_INFO:
            error = "Choose a service"
        elif choice == "express" and q.get("hazardous"):
            error = "Hazardous goods cannot fly Express"
        else:
            q["service"] = choice
            _save_quote(request, q)
            return redirect("freight:quote_review")
    db.query(request, 1)                # rate tables
    today = catalog.today()
    options = []
    for key, info in pricing.SERVICE_INFO.items():
        quote = _priced(q, release, service=key, with_promo=False)
        days = catalog.TRANSIT_DAYS[key]
        options.append({"key": key, "label": info["label"], "days": info["days"],
                        "total": pricing.fmt(quote.total),
                        "eta": catalog.business_days(today, days),
                        "disabled": key == "express" and q.get("hazardous")})
    return _wizard(request, "service", "freight/quote_service.html",
                   {"options": options, "error": error,
                    "chosen": q.get("service", "standard"),
                    "status": 400 if error else 200})


@freight_view
def quote_review(request):
    release = release_of(request)
    q = _quote_state(request)
    missing = _first_missing(q, release)
    if missing:
        return redirect(f"freight:quote_{missing}")
    errors = {}
    contact = {"name": "", "email": ""}
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "promo":
            code = (request.POST.get("promo") or "").strip().upper()
            if code in pricing.PROMOS:
                q["promo"] = code
                _save_quote(request, q)
                flash(request, f"Promo code {code} applied")
                return redirect("freight:quote_review")
            errors["promo"] = f"'{code}' is not a valid promo code" if code else "Enter a promo code"
        elif action == "remove-promo":
            q.pop("promo", None)
            _save_quote(request, q)
            return redirect("freight:quote_review")
        elif action == "book":
            contact = {"name": (request.POST.get("contact_name") or "").strip(),
                       "email": (request.POST.get("contact_email") or "").strip()}
            if not contact["name"]:
                errors["contact_name"] = "Who should we contact about this shipment?"
            if "@" not in contact["email"] or "." not in contact["email"].split("@")[-1]:
                errors["contact_email"] = "Enter an email address"
            if not errors:
                quote = _priced(q, release)
                bid = save_booking({
                    "created": dt.datetime.now().isoformat(timespec="minutes"),
                    "contact": contact["name"], "email": contact["email"],
                    "origin": q["origin"], "destination": q["destination"],
                    "service": pricing.SERVICE_INFO[q["service"]]["label"],
                    "weight_kg": q["weight_kg"], "pieces": q.get("pieces", 1),
                    "total": pricing.fmt(quote.total), "promo": quote.promo,
                    "eta": catalog.business_days(
                        catalog.today(), catalog.TRANSIT_DAYS[q["service"]]).isoformat(),
                    "status": "Booked", "release": release.version,
                })
                request.session.pop("freight_quote", None)
                return redirect("freight:quote_booked", booking_id=bid)
    quote = _priced(q, release)
    return _wizard(request, "review", "freight/quote_review.html", {
        "quote": quote,
        "amounts": {k: pricing.fmt(getattr(quote, k))
                    for k in ("subtotal", "discount", "fuel", "total")},
        "lines": [{"label": l.label, "amount": pricing.fmt(l.amount)} for l in quote.lines],
        "service_label": pricing.SERVICE_INFO[q["service"]]["label"],
        "customs": q.get("customs") if (release.flags["customs_step"]
                                        and q.get("international")) else None,
        "errors": errors, "contact": contact,
        "status": 400 if errors else 200,
    })


@freight_view
def quote_booked(request, booking_id):
    b = booking(booking_id)
    if b is None:
        raise Http404("no such booking")
    return page(request, "freight/quote_booked.html", {"booking": b})


# --------------------------------------------------------------------------
# sign-in
# --------------------------------------------------------------------------

@freight_view
def login_view(request):
    release = release_of(request)
    # A session exists BEFORE authenticating, as in most real apps (it holds
    # 'next', a CSRF nonce, a locale...). That is what makes fixation possible.
    request.session.setdefault("freight_visited", True)
    target = request.POST.get("next") or request.GET.get("next") or ""
    error = ""
    username = (request.GET.get("user") or "").strip().lower()
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip().lower()
        if accounts.check_password(username, request.POST.get("password")):
            if release.flags["rotate_session"]:
                request.session.cycle_key()      # the line 2.1.0 lost
            request.session["freight_user"] = username
            return redirect(safe_next(request, target))
        error = "Wrong username or password"
    return page(request, "freight/login.html",
                {"error": error, "next": target, "users": USERS,
                 "prefill": username if username in USERS else ""},
                status=401 if error else 200)


@freight_view
def logout_view(request):
    request.session.pop("freight_user", None)
    request.session.pop("freight_changes", None)
    # A NEW session id, and the old one deleted server-side: a replayed
    # cookie must not work. cycle_key (not flush) keeps the hub's own
    # password-gate sign-in alive in the same browser.
    request.session.cycle_key()
    flash(request, "You are signed out")
    return redirect("freight:login")


# --------------------------------------------------------------------------
# staff portal
# --------------------------------------------------------------------------

@freight_view
@login_required()
def ops_dashboard(request):
    db.query(request, 3)
    today = catalog.today()
    ships = catalog.shipments()
    recent = [s for s in ships if s.created.date() > today - dt.timedelta(days=30)]
    statuses = Counter(_status_of(request, s) for s in ships)
    delivered = [s for s in recent if _status_of(request, s) == "delivered" and s.on_time is not None]
    daily = Counter(s.created.date() for s in recent)
    days = [today - dt.timedelta(days=i) for i in range(29, -1, -1)]
    weeks = []
    for w in range(5, -1, -1):
        start, end = today - dt.timedelta(days=7 * (w + 1)), today - dt.timedelta(days=7 * w)
        done = [s for s in ships if s.delivered and start < s.delivered.date() <= end]
        if done:
            weeks.append({"label": end.strftime("%b %d"),
                          "pct": round(100.0 * sum(1 for s in done if s.on_time) / len(done), 1)})
    lanes = Counter((s.origin_hub.code, s.dest_hub.code) for s in recent)
    user = request.freight_user
    return page(request, "freight/ops_dashboard.html", {
        "section": "dashboard",
        "kpis": {
            "active": sum(statuses[k] for k in ("booked", "picked_up", "at_hub",
                                                "in_transit", "out_for_delivery")),
            "on_time_pct": round(100.0 * sum(1 for s in delivered if s.on_time) / len(delivered), 1)
            if delivered else None,
            "exceptions": statuses["exception"],
            "revenue": pricing.fmt(sum((s.price for s in recent), Decimal("0")))
            if user["role"] == "manager" else None,
        },
        "charts": {
            "daily": {"labels": [d.strftime("%b %d") for d in days],
                      "values": [daily.get(d, 0) for d in days]},
            "on_time": {"labels": [w["label"] for w in weeks],
                        "values": [w["pct"] for w in weeks]},
            "services": {"labels": [pricing.SERVICE_INFO[k]["label"] for k in catalog.SERVICES],
                         "values": [sum(1 for s in recent if s.service == k)
                                    for k in catalog.SERVICES]},
        },
        "exceptions": [s for s in ships if _status_of(request, s) == "exception"][:6],
        "top_lanes": [{"lane": f"{a} -> {b}", "count": n} for (a, b), n in lanes.most_common(5)],
    })


SORTS = {
    "id": lambda s: s.id, "customer": lambda s: s.customer,
    "origin": lambda s: s.origin.name, "destination": lambda s: s.destination.name,
    "service": lambda s: s.service, "created": lambda s: s.created,
    "eta": lambda s: s.eta, "weight": lambda s: s.weight_kg,
}


@freight_view
@login_required()
def ops_shipments(request):
    g = request.GET
    query = (g.get("q") or "").strip()
    status = g.get("status", "")
    service = g.get("service", "")
    sort = g.get("sort", "-created")
    key = sort.lstrip("-")
    if key not in SORTS and key != "status":
        sort, key = "-created", "created"
    db.query(request, 2)                  # count + page
    rows = []
    needle = query.lower()
    for s in catalog.shipments():
        st = _status_of(request, s)
        if status and st != status:
            continue
        if service and s.service != service:
            continue
        if needle and not (needle in s.id.lower() or needle in s.customer.lower()
                           or needle in s.origin.name.lower()
                           or needle in s.destination.name.lower()):
            continue
        rows.append((s, st))
    if key == "status":
        rows.sort(key=lambda r: STATUS_ORDER[r[1]], reverse=sort.startswith("-"))
    else:
        rows.sort(key=lambda r: SORTS[key](r[0]), reverse=sort.startswith("-"))
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page_no = _int(g.get("page"), 1, 1, pages)
    start = (page_no - 1) * PAGE_SIZE
    shown = [{"s": s, "status": st, "status_label": catalog.STATUS_LABELS[st],
              "service": pricing.SERVICE_INFO[s.service]["label"]}
             for s, st in rows[start:start + PAGE_SIZE]]
    base = {"q": query, "status": status, "service": service}
    return page(request, "freight/ops_shipments.html", {
        "section": "shipments", "rows": shown, "total": total,
        "first": start + 1 if total else 0, "last": min(total, start + PAGE_SIZE),
        "page_no": page_no, "pages": pages, "sort": sort, "filters": base,
        "statuses": [(k, catalog.STATUS_LABELS[k]) for k in catalog.STATUSES],
        "services": [(k, pricing.SERVICE_INFO[k]["label"]) for k in catalog.SERVICES],
        "columns": [("id", "Shipment"), ("customer", "Customer"), ("origin", "From"),
                    ("destination", "To"), ("service", "Service"),
                    ("status", "Status"), ("created", "Booked"), ("eta", "ETA")],
    })


@freight_view
@login_required()
def ops_shipment(request, sid):
    s = catalog.shipment(sid)
    if s is None:
        raise Http404("no such shipment")
    if request.method == "POST":
        changes = _changes(request)
        entry = changes.setdefault(s.id, {})
        user = request.freight_user
        now = dt.datetime.now().isoformat(timespec="seconds")
        action = request.POST.get("action")
        if action == "deliver" and entry.get("status", s.status) != "delivered":
            entry["status"] = "delivered"
            entry.setdefault("events", []).append(
                {"code": "delivered", "when": now, "where": catalog.label(s.destination),
                 "text": f"Marked delivered by {user['name']}"})
            flash(request, f"{s.id} marked as delivered")
        elif action == "flag":
            reason = request.POST.get("reason", "")
            reasons = dict(EXCEPTION_REASONS)
            if reason in reasons and entry.get("status", s.status) not in ("delivered", "exception"):
                entry["status"] = "exception"
                entry.setdefault("events", []).append(
                    {"code": "exception", "when": now, "where": catalog.label(s.origin_hub.city),
                     "text": f"{reasons[reason]} (flagged by {user['name']})"})
                flash(request, f"{s.id} flagged: {reasons[reason]}", "warn")
            else:
                flash(request, "Choose a reason to flag the shipment", "error")
        request.session["freight_changes"] = changes
        return redirect("freight:shipment", sid=s.id)
    db.query(request, 1)
    return page(request, "freight/ops_shipment.html", {
        "section": "shipments", "ship": _shipment_ctx(request, s),
        "reasons": EXCEPTION_REASONS,
    })


EXCEPTION_REASONS = [
    ("damaged", "Damaged packaging"), ("address", "Address problem"),
    ("customs", "Customs documents missing"), ("weather", "Weather delay"),
]


@freight_view
@login_required()
def ops_optimizer(request):
    release = release_of(request)
    values = {"region": "california", "stops": 25, "trucks": 4, "seed": 42}
    errors = {}
    if request.method == "POST":
        p = request.POST
        values = {"region": p.get("region", ""),
                  "stops": _int(p.get("stops"), 0, 0, 999),
                  "trucks": _int(p.get("trucks"), 0, 0, 999),
                  "seed": _int(p.get("seed"), 42, 0, 10 ** 9)}
        if values["region"] not in REGIONS:
            errors["region"] = "Choose a region"
        lo, hi = optimizer.STOPS_RANGE
        if not lo <= values["stops"] <= hi:
            errors["stops"] = f"Between {lo} and {hi} stops"
        lo, hi = optimizer.TRUCKS_RANGE
        if not lo <= values["trucks"] <= hi:
            errors["trucks"] = f"Between {lo} and {hi} trucks"
        if not errors:
            label = REGIONS[values["region"]]["label"]
            job = jobs.submit(
                "optimize", f"Route plan: {label}, {values['stops']} stops, "
                            f"{values['trucks']} trucks",
                optimizer.run, dict(values, step_s=release.flags["optimizer_step_s"]),
                release.version)
            return redirect("freight:job", job_id=job.id)
    return page(request, "freight/ops_optimizer.html", {
        "section": "optimizer", "values": values, "errors": errors,
        "regions": [(k, r["label"]) for k, r in REGIONS.items()],
        "recent": jobs.recent("optimize"),
    }, status=400 if errors else 200)


@freight_view
@login_required()
def ops_import(request):
    release = release_of(request)
    error = ""
    if request.method == "POST":
        if request.POST.get("action") == "sample":
            name, data = "sample_manifest.csv", (catalog.DATA_DIR / "sample_manifest.csv").read_bytes()
        else:
            upload = request.FILES.get("manifest")
            name = upload.name if upload else ""
            data = upload.read(manifest.MAX_BYTES + 1) if upload else b""
        if not data:
            error = "Choose a CSV file to import"
        else:
            try:
                rows = manifest.parse(data)
            except manifest.ManifestError as exc:
                error = str(exc)
            else:
                job = jobs.submit("manifest", f"Manifest import: {name} ({len(rows)} rows)",
                                  manifest.run,
                                  {"rows": rows, "filename": name,
                                   "step_s": release.flags["manifest_row_s"]},
                                  release.version)
                return redirect("freight:job", job_id=job.id)
    return page(request, "freight/ops_import.html", {
        "section": "import", "error": error, "columns": manifest.COLUMNS,
        "recent": jobs.recent("manifest"),
    }, status=400 if error else 200)


@freight_view
@login_required(role="manager")
def ops_reports(request):
    release = release_of(request)
    months = reports.available_months()
    values = {"month": months[0][0] if months else "", "detail": "summary"}
    error = ""
    if request.method == "POST":
        values = {"month": request.POST.get("month", ""),
                  "detail": request.POST.get("detail", "summary")}
        if values["month"] not in [m for m, _ in months]:
            error = "Choose a month"
        elif values["detail"] not in reports.DETAIL:
            error = "Choose summary or full"
        else:
            label = dict(months)[values["month"]]
            job = jobs.submit("report", f"Operations report: {label} ({values['detail']})",
                              reports.run,
                              dict(values, unit_s=release.flags["report_unit_s"]),
                              release.version)
            return redirect("freight:job", job_id=job.id)
    return page(request, "freight/ops_reports.html", {
        "section": "reports", "months": months, "values": values, "error": error,
        "recent": jobs.recent("report"),
    }, status=400 if error else 200)


def _job_result_ctx(job):
    result = job.result or {}
    if job.kind == "optimize":
        proj = geo.projection(result["region"])
        dx, dy = proj.xy(*result["depot_point"])
        return {"plan": result, "map": {
            "w": proj.width, "h": proj.height,
            "basemap": f"{reverse('freight:basemap', args=[result['region']])}?theme="
                       f"{releases.deployed().flags['theme']}",
            "depot": {"x": dx, "y": dy, "rx": round(dx - 7, 1), "ry": round(dy - 7, 1),
                      "name": result["depot"]},
            "routes": [{"truck": r["truck"], "color": r["color"], "km": r["km"],
                        "points": geo.polyline(proj, r["path"])} for r in result["routes"]],
            "stops": [{"name": n, "x": proj.xy(lat, lon)[0], "y": proj.xy(lat, lon)[1],
                       "demand": d} for n, lat, lon, d in result["stop_points"]],
        }}
    if job.kind == "manifest":
        return {"imported": dict(result, total_fmt=pricing.fmt(result["total"]))}
    if job.kind == "report":
        k = result["kpis"]
        return {"report": result, "report_revenue": pricing.fmt(k["revenue"]),
                "report_charts": {
                    "daily": {"labels": [d["date"][5:] for d in result["daily"]],
                              "values": [d["shipments"] for d in result["daily"]]},
                    "services": {"labels": [r["service"].title() for r in result["by_service"]],
                                 "values": [r["shipments"] for r in result["by_service"]]}},
                "fmt_rows": {
                    "lanes": [dict(r, revenue=pricing.fmt(r["revenue"])) for r in result["top_lanes"]],
                    "customers": [dict(r, revenue=pricing.fmt(r["revenue"]))
                                  for r in result["top_customers"]],
                    "services": [dict(r, revenue=pricing.fmt(r["revenue"]))
                                 for r in result["by_service"]]}}
    return {}


@freight_view
@login_required()
def ops_job(request, job_id):
    job = jobs.get(job_id)
    if job is None:
        return page(request, "freight/job_gone.html", {"section": "jobs"}, status=404)
    ctx = {"section": {"optimize": "optimizer", "manifest": "import",
                       "report": "reports"}.get(job.kind, ""),
           "job": job, "snap": job.snapshot()}
    if job.state == jobs.DONE:
        ctx.update(_job_result_ctx(job))
    return page(request, "freight/job.html", ctx)


@freight_view
@login_required()
def ops_job_download(request, job_id):
    job = jobs.get(job_id)
    if job is None or job.state != jobs.DONE:
        raise Http404("no finished job with that id")
    if job.kind == "optimize":
        body, name = optimizer.plan_csv(job.result), f"route-plan-{job.id}.csv"
    elif job.kind == "manifest":
        body, name = manifest.results_csv(job.result), f"manifest-results-{job.id}.csv"
    else:
        body, name = reports.report_csv(job.result), f"report-{job.result['month']}.csv"
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@freight_view
@login_required()
def ops_docks(request):
    """Drag arrivals onto dock doors (JS), or use the per-card form (no JS,
    and the Remote Recorder): both land on the same server-side rules."""
    if request.method == "POST":
        action = request.POST.get("action", "")
        arrival = request.POST.get("arrival", "")
        if action == "assign":
            door, _, slot = (request.POST.get("cell") or "").partition("|")
            error = docks.assign(request.session, arrival, door, slot)
            if error:
                flash(request, error, "error")
            else:
                flash(request, f"{arrival} booked at {door} {slot}")
        elif action == "unassign":
            docks.unassign(request.session, arrival)
        elif action == "auto":
            docks.auto(request.session)
            flash(request, "Auto-scheduled everything that fits")
        elif action == "clear":
            docks.clear(request.session)
        return redirect("freight:docks")
    rows, waiting = docks.board(request.session)
    return page(request, "freight/ops_docks.html", {
        "section": "docks", "rows": rows, "waiting": waiting,
        "doors": docks.DOORS, "slots": docks.SLOTS,
        "cells": [(f"{d}|{s}", f"{n} at {s}") for s in docks.SLOTS for d, n, _ in docks.DOORS],
    })


@freight_view
@login_required()
def ops_fleet(request):
    from freight.regions import REGIONS
    region = request.GET.get("region", "california")
    if region not in REGIONS:
        region = "california"
    proj = geo.projection(region)
    snap = fleet.state(region)
    return page(request, "freight/ops_fleet.html", {
        "section": "fleet", "region": region,
        "regions": [(k, r["label"]) for k, r in REGIONS.items()],
        "map": {"w": proj.width, "h": proj.height,
                "basemap": _basemap_url(region, release_of(request))},
        "routes": fleet.route_lines(region), "snap": snap,
    })


# --------------------------------------------------------------------------
# release console, tour, static-ish bits
# --------------------------------------------------------------------------

def _when(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime("%b %d, %H:%M:%S")
    except ValueError:
        return iso


@freight_view
@incident_exempt
def releases_page(request):
    if request.method == "POST" and request.POST.get("action") == "incident":
        user = current_user(request)
        kind = request.POST.get("incident", "")
        try:
            releases.set_incident(None if kind == "clear" else kind,
                                  by=user["name"] if user else "release console")
        except ValueError as exc:
            flash(request, str(exc), "error")
        else:
            flash(request, releases.INCIDENTS.get(kind, "All clear -- the incident is over"),
                  "ok" if kind == "clear" else "warn")
        return redirect("freight:releases")
    if request.method == "POST":
        version = request.POST.get("version", "")
        user = current_user(request)
        try:
            rel = releases.deploy(version, by=user["name"] if user else "release console")
        except ValueError:
            flash(request, f"There is no release {version!r}", "error")
        else:
            request.freight_release = rel      # this response already speaks the new version
            flash(request, f"Deployed {rel.version} ({rel.name})")
        return redirect("freight:releases")
    cfg = appconfig.get_config()
    live = release_of(request)
    return page(request, "freight/releases.html", {
        "all": [{"r": r, "live": r.version == live.version} for r in releases.RELEASES],
        "history": [dict(h, when=_when(h.get("at", ""))) for h in releases.deploy_history()[:12]],
        "hub_is_demo": cfg.target_is_demo,
        "hub_stamp": cfg.target_version,
        "incident": releases.incident(),
        "incidents": releases.INCIDENTS,
    })


@freight_view
def developers(request):
    """The public API's documentation, with a live example response."""
    release = release_of(request)
    base = request.build_absolute_uri(reverse("freight:api_v1_status")).rsplit("status", 1)[0]
    sample = public_api.shipment_json(catalog.shipment("AF-100001"), release)
    import json as _json
    return page(request, "freight/developers.html", {
        "base": base, "keys": public_api.API_KEYS,
        "sample": _json.dumps(sample, indent=2)[:2400],
        "changelog": [(v, text) for v, text in public_api.CHANGELOG
                      if tuple(map(int, v.split("."))) <= tuple(map(int, release.version.split(".")))],
    })


@freight_view
def tour(request):
    return page(request, "freight/tour.html", {"releases": releases.RELEASES})


@freight_view
def basemap(request, key):
    try:
        svg = geo.basemap_svg(key, request.GET.get("theme", "classic"))
    except KeyError:
        raise Http404("no such map")
    response = HttpResponse(svg, content_type="image/svg+xml")
    response["Cache-Control"] = "public, max-age=86400"
    return response


@freight_view
def sample_manifest(request):
    response = FileResponse(open(catalog.DATA_DIR / "sample_manifest.csv", "rb"),
                            content_type="text/csv; charset=utf-8",
                            as_attachment=True, filename="sample_manifest.csv")
    return response


# --------------------------------------------------------------------------
# password reset by email -- and the mailbox that catches the email
# --------------------------------------------------------------------------

def _reset_link(request):
    return lambda token: request.build_absolute_uri(reverse("freight:reset", args=[token]))


@freight_view
def forgot_password(request):
    ref, login, error = None, "", ""
    if request.method == "POST":
        login = (request.POST.get("login") or "").strip()
        if not login:
            error = "Enter your username or email address"
        else:
            ref = accounts.start_reset(accounts.find(login), _reset_link(request))
    return no_store(page(request, "freight/forgot.html",
                         {"ref": ref, "login": login, "error": error},
                         status=400 if error else 200))


@freight_view
def reset_password(request, token):
    release = release_of(request)
    state, username = accounts.reset_state(token)
    error = ""
    if state == "ok" and request.method == "POST":
        password, confirm = request.POST.get("password", ""), request.POST.get("confirm", "")
        error = accounts.password_problem(username, password, confirm) or ""
        if not error:
            try:
                accounts.finish_reset(token, password,
                                      single_use=release.flags["reset_single_use"])
            except ValueError:
                state = "used"            # a second submit of the same link won the race
            else:
                flash(request, "Your password is changed. Sign in with the new one.")
                return redirect(f"{reverse('freight:login')}?user={username}")
    if state != "ok":
        response = page(request, "freight/reset_invalid.html", {"state": state},
                        status=404 if state == "unknown" else 410)
    else:
        response = page(request, "freight/reset.html",
                        {"username": username, "error": error,
                         "shared": username in accounts.SHARED,
                         "min_length": accounts.MIN_LENGTH},
                        status=400 if error else 200)
    # The token is in this page's URL: never hand it to another site. Not
    # "no-referrer": under that policy browsers send `Origin: null` with the
    # page's own form POST, and Django's CSRF check rejects it (403) -- the
    # reset could never be submitted. same-origin keeps both.
    response["Referrer-Policy"] = "same-origin"
    return no_store(response)


@freight_view
def mailbox(request):
    to = (request.GET.get("to") or "").strip().lower()
    messages = [{"id": m.id, "to": m.to, "subject": m.subject,
                 "received": dt.datetime.fromtimestamp(m.deliver_at)}
                for m in mail.delivered(to=to)[:100]]
    return no_store(page(request, "freight/mailbox.html", {
        "messages": messages, "to": to, "newest": messages[0]["id"] if messages else 0,
        "addresses": [accounts.email_of(u) for u in USERS],
    }))


@freight_view
def mailbox_message(request, msg_id):
    msg = mail.get(msg_id)
    if msg is None:
        raise Http404("no such message")
    return no_store(page(request, "freight/mail_message.html", {
        "msg": msg, "sender": mail.SENDER,
        "received": dt.datetime.fromtimestamp(msg.deliver_at)}))


# --------------------------------------------------------------------------
# shipping labels: an HTML page to print (opens in a new tab) and a PDF
# --------------------------------------------------------------------------

SERVICE_CODES = {"economy": "ECO", "standard": "STD", "express": "EXP"}


def _label_fields(s):
    return {
        "id": s.id, "customer": s.customer, "code": SERVICE_CODES[s.service],
        "service": pricing.SERVICE_INFO[s.service]["label"],
        "from": catalog.label(s.origin), "to": catalog.label(s.destination),
        "route": f"{s.origin_hub.code} -> {s.dest_hub.code}",
        "weight": f"{s.weight_kg:g} kg", "pieces": s.pieces,
        "created": s.created.strftime("%Y-%m-%d"),
    }


def _labelled_shipment(sid):
    s = catalog.shipment(sid)
    if s is None:
        raise Http404("no such shipment")
    return s


@freight_view
@login_required()
def ops_label(request, sid):
    s = _labelled_shipment(sid)
    return page(request, "freight/label.html", {
        "ship": _shipment_ctx(request, s), "fields": _label_fields(s),
        "barcode": mark_safe(shiplabel.svg(s.id)),
    })


@freight_view
@login_required()
def ops_label_pdf(request, sid):
    s = _labelled_shipment(sid)
    response = HttpResponse(shiplabel.pdf(_label_fields(s)), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="label-{s.id}.pdf"'
    return response


# --------------------------------------------------------------------------
# the public status page (up even when the site is not)
# --------------------------------------------------------------------------

@freight_view
@incident_exempt
def status_page(request):
    return page(request, "freight/status.html", {"s": statuspage.summary()})


@freight_view
@incident_exempt
def status_json(request):
    s = statuspage.summary()
    return JsonResponse({
        "status": s["state"], "headline": s["headline"],
        "since": s["since"].isoformat(timespec="seconds") if s["since"] else None,
        "components": [{"key": c["key"], "name": c["name"], "status": c["state"]}
                       for c in s["components"]],
        "uptime_30d_percent": s["uptime"],
    })

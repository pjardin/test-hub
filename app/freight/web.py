"""Plumbing shared by the demo's pages and its JSON API: the release that
applies to a request, its response headers, sign-in, flash messages, the
simulated database, and the in-memory bookings.

The release is looked up once per request and decides everything a version
changes -- theme, speed, security headers, deliberate bugs -- so a page
never has to know which release it is serving.
"""
import functools
import itertools
import secrets
import threading
import time
from collections import OrderedDict

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from freight import jobs, releases
from freight.accounts import PASSWORD, USERS  # noqa: F401 -- views import them from here

# Content-Security-Policy of every healthy release. No inline scripts
# anywhere in the demo, so script-src can be 'self' alone; inline STYLE
# attributes are allowed (progress bars, map colours).
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors 'none'")

# Staff accounts (USERS, PASSWORD) live in accounts.py, with password resets
# by email. Demo credentials, printed on the sign-in page on purpose; the
# sample tests read them with ctx.secret("demo_password", ...).


# --------------------------------------------------------------------------
# the release, and the headers it sends
# --------------------------------------------------------------------------

def release_of(request):
    rel = getattr(request, "freight_release", None)
    if rel is None:
        rel = releases.deployed()
        request.freight_release = rel
    return rel


def apply_release_headers(response, release):
    response["X-Demo-Version"] = release.version
    if release.flags["csp"]:
        response["Content-Security-Policy"] = CSP
    if not release.flags["frame_protection"]:
        # the 2.1.0 regression: XFrameOptionsMiddleware leaves this alone
        response.xframe_options_exempt = True
    return response


SLOW_SECONDS = 2.5


def freight_view(view):
    """Every demo view: resolve the release once, honour a simulated
    incident, stamp the release's headers on the way out."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        release_of(request)
        kind = releases.incident()
        if kind and not getattr(view, "incident_exempt", False):
            if kind == "outage":
                return apply_release_headers(_maintenance(request), release_of(request))
            time.sleep(SLOW_SECONDS * jobs.TIME_SCALE)          # "slow"
        response = view(request, *args, **kwargs)
        # read it AGAIN: a deploy inside the view (the Release console)
        # means this very response already belongs to the new release
        return apply_release_headers(response, release_of(request))
    return wrapper


def incident_exempt(view):
    """The Release console stays up during a simulated outage -- otherwise
    nobody could switch the outage off again."""
    view.incident_exempt = True
    return view


def _maintenance(request):
    if "/api/" in request.path:
        response = JsonResponse({"error": "Acme Freight is down for maintenance"},
                                status=503)
    else:
        response = render(request, "freight/maintenance.html",
                          {"release": release_of(request), "theme": release_of(request).flags["theme"]},
                          status=503)
    response["Retry-After"] = "300"
    return response


# --------------------------------------------------------------------------
# sign-in
# --------------------------------------------------------------------------

def current_user(request):
    key = request.session.get("freight_user")
    if key in USERS:
        return dict(USERS[key], username=key)
    return None


def login_required(role=None):
    def deco(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            user = current_user(request)
            if user is None:
                return redirect(f"{reverse('freight:login')}?next={request.path}")
            if role and user["role"] != role:
                return page(request, "freight/forbidden.html",
                            {"needed": role}, status=403)
            request.freight_user = user
            return no_store(view(request, *args, **kwargs))
        return wrapper
    return deco


def no_store(response):
    """Signed-in pages must not linger in the browser cache: on a shared
    workstation the next person presses Back and reads them. (The hub's
    security observer checks exactly this on authenticated pages.)"""
    response["Cache-Control"] = "no-store"
    return response


def safe_next(request, target):
    """Only ever redirect back INTO the demo after sign-in."""
    home = reverse("freight:home")
    if (target and target.startswith(home)
            and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()})):
        return target
    return reverse("freight:ops")


# --------------------------------------------------------------------------
# rendering + flash messages (kept apart from the hub's own messages)
# --------------------------------------------------------------------------

def flash(request, text, level="ok"):
    items = request.session.get("freight_flash", [])
    items.append([level, text])
    request.session["freight_flash"] = items[-5:]


def page(request, template, context=None, status=200):
    release = release_of(request)
    ctx = {
        "release": release,
        "flags": release.flags,
        "theme": release.flags["theme"],
        "user": current_user(request),
        "flashes": request.session.pop("freight_flash", []) if "freight_flash" in request.session else [],
    }
    ctx.update(context or {})
    return render(request, template, ctx, status=status)


# --------------------------------------------------------------------------
# the simulated database: a cost per query and a connection pool
# --------------------------------------------------------------------------

class _Database:
    """Each query holds a pool slot for the release's search_ms. Beyond
    db_pool concurrent queries, callers WAIT for a slot -- which is exactly
    how a real app degrades under load, and what a load test's per-step
    breakdown is there to expose."""

    def __init__(self):
        self._lock = threading.Lock()
        self._size = None
        self._sem = None

    def _pool(self, size):
        with self._lock:
            if size != self._size:
                self._size, self._sem = size, threading.BoundedSemaphore(size)
            return self._sem

    def query(self, request, n=1):
        flags = release_of(request).flags
        pool = self._pool(flags["db_pool"])
        for _ in range(n):
            with pool:
                time.sleep(flags["search_ms"] / 1000.0 * jobs.TIME_SCALE)


db = _Database()


# --------------------------------------------------------------------------
# bookings made through the quote wizard (in memory, bounded)
# --------------------------------------------------------------------------

_bookings = OrderedDict()
_bookings_lock = threading.Lock()
MAX_BOOKINGS = 2000


def save_booking(booking):
    with _bookings_lock:
        while True:
            bid = f"AF-9{secrets.randbelow(100000):05d}"
            if bid not in _bookings:
                break
        booking["id"] = bid
        _bookings[bid] = booking
        while len(_bookings) > MAX_BOOKINGS:
            _bookings.popitem(last=False)
    return bid


def booking(bid):
    with _bookings_lock:
        return _bookings.get(str(bid or "").strip().upper())


# every Nth note save fails on a release with note_fail_every = N
_note_counter = itertools.count(1)
_note_lock = threading.Lock()


def next_note_fails(release):
    every = release.flags["note_fail_every"]
    with _note_lock:
        n = next(_note_counter)
    return bool(every) and n % every == 0

"""Staff accounts, their passwords, and password resets by email.

Everyone signs in with the published demo password until they change it
through "Forgot your password?" -- which sends a real (caught) email with a
single-use link. Changed passwords live in memory: a restart puts every
account back on the published password.

The sample tests sign in as tester and manager, so those two are SHARED:
their password can never be changed here, or one curious click in a demo
breaks every other test. The accounts `driver` (the Python reset test) and
`courier` (the TypeScript one) are the ones to reset.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

from freight import mail

PASSWORD = "demo-password"
USERS = {
    "tester": {"name": "Terry Tester", "role": "dispatcher"},
    "manager": {"name": "Morgan Manager", "role": "manager"},
    "driver": {"name": "Drew Driver", "role": "driver"},
    # a second account to reset, so the TypeScript reset test (DEMO-T16) and
    # the Python one (DEMO-023) can run side by side without changing each
    # other's password mid-test
    "courier": {"name": "Casey Courier", "role": "driver"},
}
SHARED = frozenset({"tester", "manager"})
EMAIL_DOMAIN = "acme-freight.example"      # reserved for examples: never routable
RESET_TTL_S = 30 * 60
MIN_LENGTH = 10
_ITERATIONS = 20000

_lock = threading.Lock()
_hashes = {}     # username -> (salt, digest), only for changed passwords
_resets = {}     # token -> {"user", "ref", "expires", "used"}


def email_of(username):
    return f"{username}@{EMAIL_DOMAIN}"


def find(login):
    """The username for what someone typed: a username or its email."""
    login = (login or "").strip().lower()
    if login in USERS:
        return login
    for name in USERS:
        if login == email_of(name):
            return name
    return None


def _digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)


def check_password(username, password):
    if username not in USERS or not isinstance(password, str):
        return False
    with _lock:
        stored = _hashes.get(username)
    if stored is None:
        return hmac.compare_digest(password.encode("utf-8"), PASSWORD.encode("utf-8"))
    salt, digest = stored
    return hmac.compare_digest(_digest(password, salt), digest)


def password_problem(username, password, confirm):
    """Why this new password is refused, or None."""
    if username in SHARED:
        return (f"{username} is a shared demo account: the sample tests sign in with its "
                f"published password, so it cannot be changed here. Try the account driver.")
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters"
    if password != confirm:
        return "The two passwords do not match"
    if check_password(username, password):
        return "That is the current password -- choose a new one"
    return None


def set_password(username, password):
    salt = os.urandom(16)
    with _lock:
        _hashes[username] = (salt, _digest(password, salt))


def start_reset(username, link_for, now=None):
    """Email a reset link to the account; returns the request reference the
    confirmation page shows. An unknown account gets a reference too (and no
    email), so the page never tells a stranger which accounts exist."""
    ref = secrets.token_hex(3).upper()
    if username not in USERS:
        return ref
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(24)
    with _lock:
        for old in [t for t, r in _resets.items() if r["expires"] < now or r["used"]]:
            del _resets[old]
        _resets[token] = {"user": username, "ref": ref, "expires": now + RESET_TTL_S,
                          "used": False}
    first = USERS[username]["name"].split()[0]
    mail.send(email_of(username), f"Reset your Acme Freight password (ref {ref})",
              f"Hello {first},\n\n"
              f"Someone -- hopefully you -- asked to reset the password of the Acme Freight "
              f"staff account \"{username}\". Open this link within {RESET_TTL_S // 60} "
              f"minutes to choose a new one:\n\n{link_for(token)}\n\n"
              f"The link works once. If you did not ask for this, ignore this email: your "
              f"password stays as it is.\n\nRequest reference: {ref}\n-- Acme Freight\n")
    return ref


def reset_state(token, now=None):
    """(state, username), state one of ok | unknown | expired | used."""
    now = time.time() if now is None else now
    with _lock:
        r = _resets.get(token)
        if r is None:
            return "unknown", None
        if r["used"]:
            return "used", r["user"]
        if r["expires"] < now:
            return "expired", r["user"]
        return "ok", r["user"]


def finish_reset(token, password, single_use=True):
    """Set the new password. The link is spent in the same locked step it is
    checked, so two submits of one link cannot both succeed -- unless the
    release skips the step (2.1.0's 'faster reset emails')."""
    now = time.time()
    with _lock:
        r = _resets.get(token)
        if r is None or r["used"] or r["expires"] < now:
            raise ValueError("this reset link is no longer valid")
        if single_use:
            r["used"] = True
        username = r["user"]
    set_password(username, password)
    return username


def reset_all():
    """Every account back on the published password (unit tests; a restart
    does the same)."""
    with _lock:
        _hashes.clear()
        _resets.clear()

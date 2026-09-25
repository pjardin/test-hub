"""Passive security observation, collected while a functional test runs.

WHAT THIS IS
The browser is already there, already loading the real application, already
seeing every response header, cookie and network request. Watching that costs
nothing, and most real-world web security findings are *configuration*
mistakes -- a missing header, a cookie without HttpOnly, a page quietly
calling out to the internet -- rather than exotic vulnerabilities.

So every test run can also answer: is this application configured the way it
should be? No extra test to write, no extra run, no separate tool.

WHAT THIS IS NOT
It is not a penetration test and not a vulnerability scanner. It does not
attack the application, try payloads, fuzz inputs, or know anything about
CVEs. It reports what the browser observed. A clean report means "no
misconfiguration was visible from here", never "this application is secure".
That distinction is stated in the UI too, because a green tick that overstates
its meaning is worse than no tick at all.

THE ONE THAT MATTERS MOST OFFLINE
On an air-gapped network, ANY request leaving for a third-party host is both
a functional bug (it will hang or fail) and a potential data path out of the
enclave. A browser sees those directly, and nothing else in the toolchain
does. `external-request` is reported as high severity for exactly that reason.

Django-free and Playwright-1.35 compatible on purpose: this runs inside the
harness subprocess, which must work on CentOS 7's older Playwright.
"""
import re
from urllib.parse import urlparse

HIGH, MEDIUM, LOW, INFO = "high", "medium", "low", "info"

# Headers a browser-facing application is expected to set, with what each one
# actually prevents -- the "why" matters more than the name to a tester.
EXPECTED_HEADERS = {
    "content-security-policy": (
        MEDIUM, "Content-Security-Policy",
        "Without it, an injected script can run with the page's full "
        "privileges. This is the single most effective header against XSS."),
    "x-content-type-options": (
        LOW, "X-Content-Type-Options: nosniff",
        "Without it, a browser may guess a response is JavaScript and run "
        "something that was only ever meant to be data."),
    "x-frame-options": (
        MEDIUM, "X-Frame-Options (or CSP frame-ancestors)",
        "Without it, another site can embed this page invisibly and trick a "
        "user into clicking things (clickjacking)."),
    "referrer-policy": (
        LOW, "Referrer-Policy",
        "Without it, the full URL of this page -- including anything "
        "sensitive in the path or query -- is sent to other sites."),
}

# Headers that hand out version numbers for free.
BANNER_HEADERS = ("server", "x-powered-by", "x-aspnet-version",
                  "x-aspnetmvc-version", "x-generator")

# Markers of an unhandled error being shown to the user. Kept narrow: a false
# "your app is leaking a stack trace" wastes somebody's afternoon.
TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    "java.lang.NullPointerException",
    "System.NullReferenceException",
    "org.springframework.web.util.NestedServletException",
    "Warning: mysqli_",
    "Fatal error: Uncaught",
    "ORA-01756",
    "You have an error in your SQL syntax",
)


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


class SecurityObserver:
    """Attaches to a Playwright page and records what it sees.

    Everything here is best-effort: a security check must never be the reason
    a functional test fails.
    """

    def __init__(self, page, base_url):
        self.base_url = base_url
        self.base_host = _host(base_url)
        self.base_scheme = (urlparse(base_url).scheme or "http").lower()
        self.main_headers = {}
        self.main_status = None
        self.main_url = ""
        self.external_hosts = {}       # host -> count
        self.insecure_requests = set()  # http:// on an https page
        self.page = page
        self._attached = False
        # authenticated-flow state, only populated if the test opts in
        self._login_seen = False
        self._fixation = []
        self._authed_cookies = []
        self._protected_anon = []
        self._protected_authed = []
        self._logout_checked = False
        self._logout_still_valid = False

    def attach(self):
        try:
            self.page.on("response", self._on_response)
            self.page.on("request", self._on_request)
            self._attached = True
        except Exception:
            pass
        return self

    # -- listeners ----------------------------------------------------------
    def _on_response(self, response):
        try:
            url = response.url
            # The first document response from the target is the one whose
            # headers describe the application.
            if not self.main_headers and _host(url) == self.base_host:
                if response.request.resource_type in ("document", ""):
                    self.main_headers = {k.lower(): v for k, v in
                                         (response.headers or {}).items()}
                    self.main_status = response.status
                    self.main_url = url
        except Exception:
            pass

    def _on_request(self, request):
        try:
            url = request.url
            if url.startswith(("data:", "blob:", "about:")):
                return
            host = _host(url)
            if host and self.base_host and host != self.base_host:
                self.external_hosts[host] = self.external_hosts.get(host, 0) + 1
            if self.base_scheme == "https" and url.startswith("http://"):
                self.insecure_requests.add(url.split("?")[0][:200])
        except Exception:
            pass

    # -- report -------------------------------------------------------------
    def report(self, page_content=""):
        """Findings, plus the raw observations they came from."""
        findings = []

        def add(severity, kind, title, detail, fix=""):
            findings.append({"severity": severity, "kind": kind, "title": title,
                             "detail": detail, "fix": fix})

        headers = self.main_headers

        # --- transport ----------------------------------------------------
        if self.base_scheme != "https":
            add(MEDIUM, "no-https", "The application is served over plain HTTP",
                f"{self.base_url} is not HTTPS, so anything typed into it "
                f"travels the network in the clear and can be modified in "
                f"transit.",
                "On an isolated lab network this may be a deliberate choice -- "
                "if so, note it and move on. Anywhere else, terminate TLS in "
                "front of the application.")

        # --- headers ------------------------------------------------------
        if headers:
            has_csp = "content-security-policy" in headers
            for key, (severity, name, why) in EXPECTED_HEADERS.items():
                if key in headers:
                    continue
                # CSP frame-ancestors is a modern replacement for X-Frame-Options
                if key == "x-frame-options" and has_csp and \
                        "frame-ancestors" in headers.get("content-security-policy", ""):
                    continue
                add(severity, "missing-header", f"Missing {name}", why,
                    f"Set the {name.split(':')[0]} response header on the "
                    f"application or its reverse proxy.")

            if self.base_scheme == "https" and \
                    "strict-transport-security" not in headers:
                add(MEDIUM, "missing-header",
                    "Missing Strict-Transport-Security",
                    "Without it, a browser can be talked into using plain HTTP "
                    "on the next visit, before the redirect happens.",
                    "Set Strict-Transport-Security on the HTTPS responses.")

            for key in BANNER_HEADERS:
                if key in headers and headers[key].strip():
                    add(LOW, "version-disclosure",
                        f"Response advertises software version ({key})",
                        f"{key}: {headers[key][:120]} -- this tells an attacker "
                        f"exactly what to look up known vulnerabilities for.",
                        f"Suppress or blank the {key} header at the proxy.")
        else:
            add(INFO, "no-headers", "No document response was observed",
                "The test never loaded a top-level page from the target, so "
                "its headers could not be checked.",
                "Have the test call page.goto(ctx.base_url) at least once.")

        # --- cookies ------------------------------------------------------
        for cookie in self._cookies():
            name = cookie.get("name", "?")
            # A CSRF token cookie is SUPPOSED to be readable by scripts -- that
            # is how the double-submit pattern works. Reporting it as a plain
            # finding sends testers chasing something that must not be
            # "fixed", so it is called out as expected instead of suppressed.
            is_csrf = "csrf" in name.lower() or "xsrf" in name.lower()
            if not cookie.get("httpOnly"):
                if is_csrf:
                    add(INFO, "cookie-flag",
                        f"Cookie {name!r} is readable by JavaScript (expected)",
                        "This looks like a CSRF token, which the page's own "
                        "JavaScript has to read in order to send it back. "
                        "HttpOnly would break that. Nothing to do -- listed so "
                        "you know it was checked and not missed.",
                        "No action. Do check that SESSION cookies are HttpOnly.")
                else:
                    add(MEDIUM, "cookie-flag",
                        f"Cookie {name!r} is readable by JavaScript",
                        "Without HttpOnly, any injected script can read this "
                        "cookie -- which is how session theft usually happens.",
                        f"Set HttpOnly on {name} "
                        f"(in Django: SESSION_COOKIE_HTTPONLY).")
            if self.base_scheme == "https" and not cookie.get("secure"):
                add(MEDIUM, "cookie-flag",
                    f"Cookie {name!r} is not marked Secure",
                    "Without Secure, the browser will send this cookie over "
                    "plain HTTP too.",
                    f"Set Secure on {name}.")
            if not cookie.get("sameSite") or cookie.get("sameSite") == "None":
                add(LOW, "cookie-flag",
                    f"Cookie {name!r} has a weak SameSite setting",
                    "SameSite limits whether the cookie is sent on requests "
                    "started by other sites, which is a large part of CSRF "
                    "defence.",
                    f"Set SameSite=Lax (or Strict) on {name}.")

        # --- the offline one ----------------------------------------------
        if self.external_hosts:
            listed = ", ".join(f"{h} ({n})" for h, n in
                               sorted(self.external_hosts.items(),
                                      key=lambda kv: -kv[1])[:8])
            add(HIGH, "external-request",
                "The page requested resources from other hosts",
                f"Requests went to: {listed}. On an air-gapped network these "
                f"cannot succeed -- so the page is either broken or waiting on "
                f"a timeout -- and each one is a path by which data could "
                f"leave the enclave.",
                "Vendor the resource locally, or remove the reference. This is "
                "worth chasing even when the page still appears to work.")

        if self.insecure_requests:
            add(HIGH, "mixed-content",
                "An HTTPS page loaded content over plain HTTP",
                f"{len(self.insecure_requests)} request(s), e.g. "
                f"{sorted(self.insecure_requests)[0]}. Browsers block or "
                f"downgrade these, and they undo the point of HTTPS.",
                "Serve every subresource over HTTPS.")

        # --- authenticated flow -------------------------------------------
        # Only reported when the test actually exercised a login: silence is
        # better than guessing about a flow that was never performed.
        for name in self._fixation:
            add(HIGH, "session-fixation",
                f"The session cookie {name!r} did not change when logging in",
                "The identifier issued before authentication is still in use "
                "afterwards. Anyone who knew the earlier value -- from a "
                "shared link, a proxy log, or by setting it themselves -- now "
                "holds an authenticated session.",
                "Regenerate the session on login (Django: "
                "django.contrib.auth.login does this; check custom flows).")

        for entry in self._protected_anon:
            # A redirect to a login page, a 401 or a 403 are all CORRECT
            # answers to an anonymous request. Only a plain 2xx means the
            # page was actually served. (Requests are made with
            # max_redirects=0 for exactly this reason: following the redirect
            # lands on the login page's own 200 and reads as a hole.)
            if 200 <= (entry["status"] or 0) < 300:
                add(HIGH, "missing-access-control",
                    "A page meant to need a login was served without one",
                    f"{entry['url']} returned HTTP {entry['status']} with no "
                    f"session cookie. If that page shows anything private, it "
                    f"is readable by anyone who knows the address.",
                    "Require authentication on the view, not only in the "
                    "navigation that links to it.")

        for entry in self._protected_authed:
            cache = entry["headers"].get("cache-control", "")
            if "no-store" not in cache:
                add(LOW, "cacheable-private-page",
                    "A logged-in page may be stored by the browser cache",
                    f"{entry['url']} did not send Cache-Control: no-store "
                    f"(saw {cache or 'nothing'!r}). On a shared workstation the "
                    f"next person can press Back and see it.",
                    "Send Cache-Control: no-store on authenticated responses.")
            break        # one example is enough; it is a site-wide setting

        # Same rule for the replayed session: 3xx/401/403 means the old
        # cookie was rejected, which is what should happen.
        if self._logout_checked and self._logout_still_valid:
            add(HIGH, "logout-not-invalidated",
                "The session still worked after logging out",
                "The old session cookie was accepted after logout, so 'log "
                "out' only cleared the browser -- the session itself is still "
                "live on the server until it expires.",
                "Invalidate the session server-side on logout, not just the "
                "cookie in the browser.")

        # --- error disclosure ---------------------------------------------
        for marker in TRACEBACK_MARKERS:
            if marker in (page_content or ""):
                add(HIGH, "error-disclosure",
                    "The page displayed an internal error",
                    f"Found {marker!r} in the rendered page. Stack traces "
                    f"reveal file paths, library versions and sometimes "
                    f"credentials.",
                    "Turn off debug mode in the application and show a generic "
                    "error page.")
                break

        return {
            "findings": findings,
            "observed": {
                "url": self.main_url or self.base_url,
                "status": self.main_status,
                "scheme": self.base_scheme,
                "headers_seen": sorted(headers.keys()),
                "external_hosts": self.external_hosts,
                "cookies": len(self._cookies()),
            },
            "counts": {
                level: sum(1 for f in findings if f["severity"] == level)
                for level in (HIGH, MEDIUM, LOW, INFO)
            },
        }

    # -- authenticated checks ----------------------------------------------
    def note_login(self, before_cookies, after_cookies):
        """Called around a login. Compares the session before and after.

        Session fixation: if the application keeps the SAME session
        identifier after authenticating, anyone who knew the pre-login value
        -- from a shared link, a proxy log, or having set it themselves --
        now holds an authenticated session. Rotating the identifier on
        privilege change is the fix, and it is a one-line setting in most
        frameworks, so it is worth knowing about.
        """
        def session_like(cookies):
            out = {}
            for cookie in cookies or []:
                name = (cookie.get("name") or "")
                if any(marker in name.lower()
                       for marker in ("session", "sid", "auth", "jsessionid")):
                    out[name] = cookie.get("value")
            return out

        before = session_like(before_cookies)
        after = session_like(after_cookies)
        self._login_seen = True
        for name, value in after.items():
            if name in before and before[name] == value and value:
                self._fixation.append(name)
        self._authed_cookies = list(after_cookies or [])

    def note_protected_response(self, url, status, headers, authenticated):
        """Record how a protected URL behaved with and without a session."""
        entry = {"url": url, "status": status,
                 "headers": {k.lower(): v for k, v in (headers or {}).items()}}
        if authenticated:
            self._protected_authed.append(entry)
        else:
            self._protected_anon.append(entry)

    def note_logout(self, still_valid):
        """Whether the old session still worked after logging out."""
        self._logout_checked = True
        self._logout_still_valid = bool(still_valid)

    def _cookies(self):
        try:
            return self.page.context.cookies() or []
        except Exception:
            return []

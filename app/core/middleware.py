"""Every page in this app carries POST-buttons (Run, Kill, Rescan...), so a
fresh browser must get a CSRF cookie on its very FIRST page view -- not only
on pages that happen to render a {% csrf_token %} form. Without this, the
first click a new user makes is a silent 403."""
from django.middleware.csrf import get_token


class EnsureCsrfCookieMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "GET" and not request.path.startswith(
                ("/static/", "/artifacts/")):
            get_token(request)  # marks the response to set the csrftoken cookie
        return self.get_response(request)


class SharedPasswordMiddleware:
    """Optional single shared password (site.password in config.json).

    Deliberately not user accounts: this is an internal tool, and on AWS the
    right answer is an ALB authenticate rule (docs/AWS.md). This exists for
    the lab case -- a hub on a shared network where 'anyone who can reach
    the port can run and edit tests' is too open. Empty password = no gate,
    which stays the default.
    """
    # /api/status/ is NOT exempt: it lists test names, batch labels and
    # activity. Load balancers get /api/health/, which says only "alive".
    #
    # These matches are ANCHORED to the configured prefix on purpose. An
    # earlier version used `path.endswith(...)` and `"/static/" in path`,
    # and both were bypassable: /tests/login/ ended with the exempt suffix,
    # and /artifacts/static/../<run>/run.log CONTAINED "/static/" -- which
    # handed every run log, screenshot and trace to anyone who could reach
    # the port. Substring matching on a path is never an access rule.
    # /demo/ is the hub's own dummy target: an inert page that exists to be
    # tested and holds no data. It must stay reachable with the gate on, or
    # the shipped sample tests (and the doctor's target check) silently test
    # the LOGIN page instead -- which "passes" a naive smoke test.
    # /demo/freight/ (Acme Freight, the richer demo) is the same kind of
    # thing: demo data only, and its own staff sign-in is part of what the
    # sample tests exercise. Prefix-anchored like /static/ -- never a
    # substring match (see above).
    EXEMPT_EXACT = ("/login/", "/api/health/", "/demo/", "/demo/submit/",
                    "/demo/login/", "/demo/private/", "/demo/logout/")
    EXEMPT_PREFIXES = ("/static/", "/demo/freight/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from core import appconfig
        cfg = appconfig.get_config()
        if not cfg.password:
            return self.get_response(request)
        if request.session.get("hub_auth") == "ok":
            return self.get_response(request)

        prefix = cfg.url_prefix          # '' or '/testhub'
        path = request.path              # already percent-decoded by Django
        if path in tuple(f"{prefix}{p}" for p in self.EXEMPT_EXACT):
            return self.get_response(request)
        if path.startswith(tuple(f"{prefix}{p}" for p in self.EXEMPT_PREFIXES)):
            return self.get_response(request)

        from django.shortcuts import redirect
        from django.urls import reverse
        return redirect(f"{reverse('login_page')}?next={path}")

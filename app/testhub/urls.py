from django.contrib import admin
from django.contrib.staticfiles.views import serve as staticfiles_serve
from django.urls import include, path, re_path
from django.views.generic import RedirectView

from core import appconfig


def _static(request, path):
    # Serve app statics straight from the source tree via the staticfiles
    # finders. insecure=True lifts the DEBUG-only guard -- deliberate: this
    # is a LAN tool served by waitress; it spares the target machines a
    # collectstatic step on every upgrade.
    return staticfiles_serve(request, path, insecure=True)


_base_patterns = [
    path("admin/", admin.site.urls),
    re_path(r"^static/(?P<path>.*)$", _static, name="static"),
    # the built-in demo site the sample tests drive (next to the classic /demo/)
    path("demo/freight/", include("freight.urls")),
    path("", include("core.urls")),
]

# site.url_prefix mounts the WHOLE app under e.g. /testhub/ so the hub can
# share a domain with the site under test (an ALB path rule forwards
# /testhub/* here WITHOUT stripping it -- so we resolve the full path, and
# every reverse()/{% url %} naturally emits the prefix). With no prefix this
# collapses to the plain layout.
_prefix = appconfig.get_config().url_prefix.strip("/")
if _prefix:
    urlpatterns = [
        path("", RedirectView.as_view(url=f"/{_prefix}/", permanent=False)),
        path(f"{_prefix}/", include(_base_patterns)),
    ]
else:
    urlpatterns = _base_patterns

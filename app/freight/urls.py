"""Acme Freight, mounted at <prefix>/demo/freight/ (testhub/urls.py)."""
from django.urls import path

from freight import api, public_api, views

app_name = "freight"

urlpatterns = [
    # public side
    path("", views.home, name="home"),
    path("track/", views.track, name="track"),
    path("quote/", views.quote_start, name="quote"),
    path("quote/route/", views.quote_route, name="quote_route"),
    path("quote/cargo/", views.quote_cargo, name="quote_cargo"),
    path("quote/customs/", views.quote_customs, name="quote_customs"),
    path("quote/service/", views.quote_service, name="quote_service"),
    path("quote/review/", views.quote_review, name="quote_review"),
    path("quote/booked/<str:booking_id>/", views.quote_booked, name="quote_booked"),
    path("login/", views.login_view, name="login"),
    path("login/forgot/", views.forgot_password, name="forgot"),
    path("login/reset/<str:token>/", views.reset_password, name="reset"),
    path("logout/", views.logout_view, name="logout"),
    path("mailbox/", views.mailbox, name="mailbox"),
    path("mailbox/<int:msg_id>/", views.mailbox_message, name="mail_message"),
    path("status/", views.status_page, name="status"),
    path("status.json", views.status_json, name="status_json"),

    # staff portal
    path("ops/", views.ops_dashboard, name="ops"),
    path("ops/shipments/", views.ops_shipments, name="shipments"),
    path("ops/shipments/<str:sid>/", views.ops_shipment, name="shipment"),
    path("ops/shipments/<str:sid>/label/", views.ops_label, name="label"),
    path("ops/shipments/<str:sid>/label.pdf", views.ops_label_pdf, name="label_pdf"),
    path("ops/optimizer/", views.ops_optimizer, name="optimizer"),
    path("ops/import/", views.ops_import, name="import"),
    path("ops/reports/", views.ops_reports, name="reports"),
    path("ops/docks/", views.ops_docks, name="docks"),
    path("ops/fleet/", views.ops_fleet, name="fleet"),
    path("ops/jobs/<str:job_id>/", views.ops_job, name="job"),
    path("ops/jobs/<str:job_id>/download/", views.ops_job_download, name="job_download"),

    # the demo's own controls
    path("releases/", views.releases_page, name="releases"),
    path("tour/", views.tour, name="tour"),
    path("developers/", views.developers, name="developers"),
    path("map/<str:key>.svg", views.basemap, name="basemap"),
    path("sample-manifest.csv", views.sample_manifest, name="sample_manifest"),

    # JSON
    path("api/version/", api.version, name="api_version"),
    path("api/cities/", api.cities, name="api_cities"),
    path("api/jobs/<str:job_id>/", api.job_status, name="api_job"),
    path("api/jobs/<str:job_id>/cancel/", api.job_cancel, name="api_job_cancel"),
    path("api/shipments/<str:sid>/notes/", api.add_note, name="api_notes"),
    path("api/docks/", api.dock_action, name="api_docks"),
    path("api/fleet/<str:region>/", api.fleet_state, name="api_fleet"),
    path("api/mailbox/", api.mailbox_state, name="api_mailbox"),

    # the PUBLIC REST API (keys, rate limit, a contract that changes by release)
    path("api/v1/status", public_api.status, name="api_v1_status"),
    path("api/v1/shipments", public_api.shipments, name="api_v1_shipments"),
    path("api/v1/shipments/<str:sid>", public_api.shipment, name="api_v1_shipment"),
    path("api/v1/quotes", public_api.quotes, name="api_v1_quotes"),
]

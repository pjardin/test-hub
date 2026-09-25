"""Freight smoke: the public site answers and tracking works.

The first test to run against a new release -- the kind of fast check a
Smoke group exists for. It proves we really are on Acme Freight (a sign-in
page or an SSO bounce also has a title), logs which release the site says it
is, and tracks two shipments: one on the move, one that does not exist.
"""
from _lib.freight import Freight


def run(page, ctx):
    site = Freight(page, ctx).open()
    assert "/demo/freight/" in page.url, f"not on the demo site: {page.url}"
    assert page.inner_text("#hero-title").strip(), "the home page has no headline"
    ctx.log(f"Acme Freight says it is release {site.version}")
    ctx.screenshot("home")

    with ctx.timed("track a shipment in transit"):
        site.track("AF-100001")
    status = page.inner_text("#track-status")
    assert status == "In transit", f"AF-100001 should be in transit; the page says {status!r}"
    assert "Frankfurt" in page.inner_text("#track-destination")
    events = page.locator("#track-events li").count()
    assert events >= 3, f"expected a scan history, found {events} events"
    ctx.screenshot("tracking")

    with ctx.timed("track an unknown number"):
        site.track("AF-000000")
    assert page.is_visible("#track-not-found"), "an unknown number must say so"

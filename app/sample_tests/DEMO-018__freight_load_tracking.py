"""A short public-tracking pass, built to be repeated UNDER LOAD.

A load test runs one test over and over in several real browsers at once,
so the test itself is short and every step is timed separately. Tracking
reads the shipments database, and the database has a limited number of
connections: with enough people tracking at once, lookups queue. The load
run's "which step got slower" table is where that shows -- and comparing a
load run on release 1.0.0 with one on 1.1.0 shows what 1.1.0's faster
database did about it.
"""
from _lib.freight import Freight


def run(page, ctx):
    site = Freight(page, ctx)
    with ctx.timed("open the home page"):
        site.open()
    with ctx.timed("track a shipment in transit"):
        site.track("AF-100001")
    with ctx.timed("track a delivered shipment"):
        site.track("AF-100002")
    assert page.inner_text("#track-status") == "Delivered"
    with ctx.timed("track an unknown number"):
        site.track("AF-000001")
    assert page.is_visible("#track-not-found")

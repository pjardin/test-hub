"""The staff workflow: search, filter, sort, open a shipment, write a note,
confirm a dialog.

The busiest screens of any back-office app are a filterable table and a
detail page with actions -- this drives both. Saving a note is a JSON
request made by the page's own JavaScript; the test waits for its OUTCOME
(the note appears, or an error does) instead of sleeping. "Mark delivered"
asks for confirmation in a browser dialog, which the test accepts.

If a release makes saving fail now and then, this is the test whose pass
rate drops -- flakiness is a signal about the SITE, and the hub's Metrics
and Analytics pages show it that way.
"""
from _lib.freight import Freight

NOTE = "Customer asked for a delivery window -- promised by Friday"


def run(page, ctx):
    site = Freight(page, ctx).sign_in()

    with ctx.timed("search shipments"):
        page.goto(site.url("ops/shipments/"))
        page.fill("#shipment-search", "Lumen")
        page.select_option("#status-filter", "in_transit")
        page.click("#apply-filters")
        page.wait_for_selector("#result-count")
    count = page.locator("#shipments-table tbody tr").count()
    assert count >= 1, "Lumen Optics has a shipment in transit (AF-100001); the search found none"
    ctx.log(page.inner_text("#result-count"))

    with ctx.timed("sort by ETA"):
        page.click("#shipments-table th a[data-sort=eta]")
        page.wait_for_selector("#shipments-table th[aria-sort] a[data-sort=eta]")

    with ctx.timed("open AF-100001"):
        page.click("#shipments-table a:text-is('AF-100001')")
        page.wait_for_selector("#ship-status")
    ctx.screenshot("shipment")

    with ctx.timed("save a note"):
        page.fill("#note-text", NOTE)
        page.click("#note-save")
        page.wait_for_selector(
            f"#notes-list .note-text:has-text('promised by Friday'), #note-error:not([hidden])",
            timeout=10000)
    if page.is_visible("#note-error"):
        ctx.screenshot("note-failed")
        raise AssertionError(f"saving a note failed: {page.inner_text('#note-error')!r}")

    page.once("dialog", lambda dialog: dialog.accept())
    with ctx.timed("mark delivered"):
        page.click("#deliver-btn")
        page.wait_for_selector("#ship-status:text-is('Delivered')")
    ctx.screenshot("delivered")
    assert page.is_visible(f"#notes-list .note-text:has-text('promised by Friday')"), \
        "the note did not survive the page reload"

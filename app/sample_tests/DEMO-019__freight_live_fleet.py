"""Wait for something that HAPPENS, on a page that never stops moving.

The live fleet map redraws every truck every two seconds, so the page is
never idle -- the hardest case for recording: a naive "save a frame when the
screen changes" would keep saving for as long as the test waits. The hub
samples a busy page sparsely while the test only waits, and its video is off
(see the test's settings), so the run stays small however long the wait
(measured: ~27 frames, under 2 MB, for a 28 s wait and for a 48 s one).

The test picks the first truck due at least 15 seconds from now -- long
enough for the hub's idle sampling (it starts after 10 s without a page
action) to show in the reel, short enough to keep the run quick --
remembers that exact arrival (the feed also lists the SAME stop from the
truck's previous lap, so "any arrival at Fresno" would pass too early), and
waits for it.
"""
from _lib.freight import Freight


def run(page, ctx):
    site = Freight(page, ctx).sign_in()
    site.open("ops/fleet/?region=california")
    page.wait_for_selector("#fleet-table tbody tr")
    ctx.screenshot("fleet")

    rows = page.eval_on_selector_all(
        "#fleet-table tbody tr[data-status='driving']",
        "rows => rows.map(r => ({truck: r.dataset.truck, stop: r.dataset.next,"
        " arrival: r.dataset.nextArrival, eta: parseInt(r.dataset.eta, 10)}))")
    assert rows, "no truck on the road -- the fleet should always be moving"
    later = [r for r in rows if r["eta"] >= 15]
    truck = min(later, key=lambda r: r["eta"]) if later else max(rows, key=lambda r: r["eta"])
    ctx.log(f"{truck['truck']} is due at {truck['stop']} in {truck['eta']}s "
            f"(arrival {truck['arrival']})")

    with ctx.timed("wait for the truck to arrive"):
        page.wait_for_selector(f"#fleet-events li[data-arrival='{truck['arrival']}']",
                               timeout=(truck["eta"] + 60) * 1000)
    ctx.screenshot("arrived")
    event = page.inner_text(f"#fleet-events li[data-arrival='{truck['arrival']}']")
    assert truck["stop"] in event, f"the arrival names the wrong place: {event!r}"
    ctx.log(f"feed: {event}")

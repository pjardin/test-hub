"""Drag-and-drop, with rules: schedule arriving trucks onto dock doors.

Drag-and-drop is where record-and-replay tools usually give up. Playwright
does it with one call -- page.drag_and_drop(card, cell) -- through real
browser drag events. The yard has rules the SERVER enforces, so the test
checks both directions: allowed drops land, a forbidden one is refused with
the reason, and the board survives a reload (it is stored, not just drawn).
"""
from _lib.freight import Freight


def run(page, ctx):
    site = Freight(page, ctx).sign_in()
    site.open("ops/docks/")
    card = lambda arrival: f".dock-card[data-arrival='{arrival}']"   # noqa: E731
    first = lambda sel: page.get_attribute(sel, "data-arrival")      # noqa: E731
    hazmat = first("#arrivals .dock-card[data-hazardous='1']")
    heavy = first("#arrivals .dock-card[data-heavy='1'][data-hazardous='0']")
    normal = first("#arrivals .dock-card[data-heavy='0'][data-hazardous='0']")
    ctx.log(f"hazmat {hazmat}, heavy {heavy}, normal {normal}")

    with ctx.timed("drag a normal load to Door 3, 09:00"):
        page.drag_and_drop(card(normal), "td[data-cell='D3|09:00']")
        page.wait_for_selector(f"td[data-cell='D3|09:00'] {card(normal)}")

    with ctx.timed("try hazardous goods at Door 1"):
        page.drag_and_drop(card(hazmat), "td[data-cell='D1|10:00']")
        page.wait_for_selector("#dock-error:not([hidden])")
    refusal = page.inner_text("#dock-error")
    ctx.screenshot("refused")
    assert "hazardous" in refusal.lower(), f"the refusal should explain why: {refusal!r}"
    assert page.locator(f"td[data-cell='D1|10:00'] {card(hazmat)}").count() == 0, \
        "a hazardous load was accepted at a non-hazmat door"

    with ctx.timed("drag hazardous goods to Door 4 (hazmat)"):
        page.drag_and_drop(card(hazmat), "td[data-cell='D4|10:00']")
        page.wait_for_selector(f"td[data-cell='D4|10:00'] {card(hazmat)}")

    with ctx.timed("drag a heavy load to a forklift door"):
        page.drag_and_drop(card(heavy), "td[data-cell='D1|11:00']")
        page.wait_for_selector(f"td[data-cell='D1|11:00'] {card(heavy)}")

    page.reload()
    for arrival, cell in ((normal, "D3|09:00"), (hazmat, "D4|10:00"), (heavy, "D1|11:00")):
        assert page.locator(f"td[data-cell='{cell}'] {card(arrival)}").count() == 1, \
            f"{arrival} is not at {cell} after a reload -- the board was not saved"
    ctx.screenshot("scheduled")

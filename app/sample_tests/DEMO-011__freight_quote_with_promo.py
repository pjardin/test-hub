"""A four-step quote, a promo code, a booking -- and checking the money.

Walks the quote wizard the way a customer does (route, cargo, service,
review), applies promo code SPRING10 and checks the discount is exactly 10%
of the subtotal -- computed from the numbers ON THE PAGE, so the test stays
right when prices change. Then it books the shipment and tracks the new
booking number.

Written in the "user-facing locators" style (get_by_label, get_by_role):
it reads like the steps a person follows, and it is what the Remote
Recorder produces too.
"""


def run(page, ctx):
    response = page.goto(ctx.base_url + "freight/quote/?restart=1")
    assert response.ok, f"the quote page answered HTTP {response.status}: {page.title()!r}"

    with ctx.timed("route"):
        page.get_by_label("From (city)").fill("San Francisco")
        page.get_by_label("To (city)").fill("San Diego")
        page.get_by_role("button", name="Next: cargo").click()

    with ctx.timed("cargo"):
        page.get_by_label("Total weight (kg)").fill("120")
        page.get_by_label("Pieces").fill("3")
        page.get_by_role("button", name="Next").click()

    with ctx.timed("choose a service"):
        page.get_by_role("radio", name="Standard").check()
        page.get_by_role("button", name="Next: review").click()

    with ctx.timed("apply promo SPRING10"):
        page.get_by_label("Promo code").fill("SPRING10")
        page.get_by_role("button", name="Apply").click()
        page.wait_for_selector("#price-discount")

    amount = lambda sel: float(page.get_attribute(sel, "data-amount"))  # noqa: E731
    subtotal, discount = amount("#price-subtotal"), amount("#price-discount")
    fuel, total = amount("#price-fuel"), amount("#price-total")
    ctx.log(f"subtotal ${subtotal:.2f}, discount ${discount:.2f}, total ${total:.2f}")
    ctx.screenshot("priced")

    expected = round(subtotal * 0.10, 2)
    assert abs(discount - expected) < 0.011, (
        f"SPRING10 should take 10% off the ${subtotal:.2f} subtotal (${expected:.2f}), "
        f"but the quote takes ${discount:.2f} off")
    assert abs(total - (subtotal - discount + fuel)) < 0.011, (
        f"the total ${total:.2f} is not subtotal - discount + fuel surcharge")

    with ctx.timed("book"):
        page.get_by_label("Contact name").fill("Grace Hopper")
        page.get_by_label("Email").fill("grace@example.gov")
        page.get_by_role("button", name="Book shipment").click()
        page.wait_for_selector("#booking-id")
    booking = page.inner_text("#booking-id")
    ctx.log(f"booked {booking}")

    with ctx.timed("track the new booking"):
        page.get_by_role("link", name="Track this shipment").click()
        page.wait_for_selector("#track-result")
    assert page.inner_text("#track-status") == "Booked"
    assert booking in page.inner_text("#track-result")

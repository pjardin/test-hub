"""Form fill and submit round-trip.

Exercises typing, selecting and clicking on the built-in demo page, then
verifies the submitted values echo back. Against your real target, replace
the selectors and assertions -- the shape stays the same.
"""


def run(page, ctx):
    page.goto(ctx.base_url)

    page.fill("#name", "Ada Lovelace")
    page.fill("#email", "ada@example.gov")
    page.select_option("#priority", "high")
    ctx.screenshot("form-filled")

    page.click("#submit-btn")
    page.wait_for_selector("#result")
    ctx.log("form submitted")
    ctx.screenshot("form-result")

    body = page.inner_text("#result")
    assert "Ada Lovelace" in body, f"submitted name missing from result: {body!r}"
    assert "high" in body, f"submitted priority missing from result: {body!r}"

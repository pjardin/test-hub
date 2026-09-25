"""Waiting for content that appears late.

The demo page reveals an element ~1.2s after a click, the way real apps
load data after an action. Playwright's wait_for_selector handles it.
"""


def run(page, ctx):
    page.goto(ctx.base_url)

    page.click("#reveal-btn")
    ctx.log("clicked reveal, waiting for delayed content")
    page.wait_for_selector("#secret", state="visible", timeout=10000)
    ctx.screenshot("revealed")

    text = page.inner_text("#secret")
    assert "delayed content" in text.lower(), f"unexpected content: {text!r}"

"""Homepage loads and renders.

The simplest possible smoke test: navigate to the target, prove we actually
landed there, and prove the page rendered and executed JavaScript.
"""


def run(page, ctx):
    page.goto(ctx.base_url)
    ctx.log(f"loaded {page.url}")
    ctx.screenshot("homepage")

    # Did we land where we asked? A site that bounces you to SSO, an error
    # page or a maintenance notice still has a title and still runs JS, so
    # without this check the rest of the test passes against the wrong page.
    # (That is not hypothetical: with the hub's password gate switched on,
    # this test used to pass while looking at the sign-in screen.)
    assert page.url.rstrip("/").startswith(ctx.base_url.rstrip("/")), (
        f"redirected away from the target: asked for {ctx.base_url}, "
        f"ended up at {page.url}")

    assert page.title(), "page has no title"
    ctx.log(f"title: {page.title()!r}")

    # Prove JS ran, not just that HTML arrived.
    assert page.evaluate("1 + 1") == 2, "JavaScript did not execute"

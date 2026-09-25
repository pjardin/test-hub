"""Log in, and let the security checks watch the session while you do.

Three things get verified along the way, none of which attack anything —
they observe a flow the application performs anyway:

  ctx.logging_in()          the session id must CHANGE when you authenticate.
                            If it does not, whoever knew the earlier value
                            now holds a logged-in session (session fixation).

  ctx.check_protected_page() the same page is fetched in a brand new browser
                            with no cookies. It must NOT come back 200.

  ctx.logging_out()         the old session cookie is replayed after logout.
                            It must no longer work.

The password comes from this machine's credential store, never from this
file — see Settings -> Credentials. Set one called `demo_password`, or the
test falls back to the demo default.
"""


def run(page, ctx):
    page.goto(f"{ctx.base_url}login/")

    with ctx.logging_in():
        page.fill("#username", "tester")
        page.fill("#password", ctx.secret("demo_password", "demo-password"))
        page.click("#login-btn")
        page.wait_for_selector("#private-heading")

    ctx.screenshot("signed-in")
    assert "tester" in page.inner_text("#who")

    # Does the private page actually require the session?
    ctx.check_protected_page()

    with ctx.logging_out():
        page.click("#logout-btn")
        page.wait_for_selector("#login-btn")

    ctx.log("signed out")

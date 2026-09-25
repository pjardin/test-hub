"""Staff sign-in, and the three session checks that matter.

  ctx.logging_in()           the session id must CHANGE at sign-in; if it
                             does not, whoever knew the old id now holds a
                             signed-in session (session fixation). This test
                             also asserts it outright, so a release that loses
                             the rotation FAILS here instead of only showing up
                             on the Security page.
  ctx.check_protected_page() the dashboard, fetched from a brand-new browser
                             with no cookies, must NOT come back 200.
  ctx.logging_out()          the old session cookie, replayed after sign-out,
                             must no longer work.

None of this attacks anything: it watches a flow the site performs anyway.
The password comes from Settings -> Credentials ("demo_password") when set.
"""
from _lib.freight import DEFAULT_PASSWORD, PASSWORD_SECRET, Freight


def _session_id(page):
    for cookie in page.context.cookies():
        if cookie["name"] == "sessionid":
            return cookie["value"]
    return ""


def run(page, ctx):
    site = Freight(page, ctx).open("login/")
    before = _session_id(page)

    with ctx.logging_in():
        page.fill("#username", "tester")
        page.fill("#password", ctx.secret(PASSWORD_SECRET, DEFAULT_PASSWORD))
        page.click("#login-btn")
        page.wait_for_selector("#signed-in-user")
    after = _session_id(page)
    ctx.screenshot("signed-in")

    assert before and after, "the site set no session cookie at all"
    assert after != before, (
        "session fixation: the session id did not change at sign-in, so anyone "
        "who knew the old id now holds a signed-in staff session")

    ctx.check_protected_page()           # the dashboard we just landed on

    with ctx.logging_out():
        site.sign_out()

    page.goto(site.url("ops/"))
    assert "/login/" in page.url, (
        f"after signing out, the staff portal should send us to sign-in; "
        f"it showed {page.url}")

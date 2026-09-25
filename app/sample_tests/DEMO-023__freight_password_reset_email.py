"""A password reset, end to end -- through the email.

The flow people test by hand because "it goes through email": ask for a
reset, WAIT for the email (a mail queue is not instant), open it, follow the
link, choose a new password, sign in with it -- and then check the link is
dead, because a reset link that works twice turns every old email into a key
to the account.

The demo catches its outgoing mail in a Mailbox page (as MailHog or Mailpit
do in a real test environment), and the test correlates by the request's
reference number: other resets may be in the mailbox too. The reset link
carries a secret token, so the test CLICKS it in the email rather than
navigating to it -- a navigation would write the token into the run's log.
The new password is random per run and never logged.
"""
import secrets

from _lib.freight import Freight

ACCOUNT = "driver"                               # tester/manager are shared: never reset
ADDRESS = "driver@acme-freight.example"
RESET_LINK = "#mail-body a[href*='/login/reset/']"


def run(page, ctx):
    site = Freight(page, ctx)
    site.open("login/")
    page.click("#forgot-link")
    page.fill("#forgot-login", ACCOUNT)
    page.click("#forgot-btn")
    ref = page.inner_text("#forgot-ref").strip()
    ctx.log(f"reset requested, reference {ref}")

    with ctx.timed("wait for the email"):
        site.open(f"mailbox/?to={ADDRESS}")
        # the mailbox checks for new mail every two seconds and reloads itself
        subject = f"#mail-list a.mail-subject:has-text('ref {ref}')"
        page.wait_for_selector(subject, timeout=60_000)
    page.click(subject)
    page.wait_for_selector(RESET_LINK)
    message = page.url
    ctx.screenshot("the reset email")

    new_password = "Pw-" + secrets.token_urlsafe(12)
    with ctx.timed("choose a new password"):
        page.click(RESET_LINK)
        page.fill("#new-password", new_password)
        page.fill("#confirm-password", new_password)
        page.click("#reset-btn")
        page.wait_for_selector("#login-btn")
    with ctx.timed("sign in with the new password"):
        page.fill("#username", ACCOUNT)
        page.fill("#password", new_password)
        page.click("#login-btn")
        page.wait_for_selector("#signed-in-user")
    ctx.log("signed in with the new password")
    page.click("#sign-out")
    page.wait_for_selector("#login-btn")

    with ctx.timed("follow the same link again"):
        page.goto(message)
        page.click(RESET_LINK)
        page.wait_for_selector("#reset-invalid, #reset-form")
    ctx.screenshot("the same link, a second time")
    assert page.locator("#reset-form").count() == 0, (
        "the reset link worked a SECOND time: any old reset email is still a key to the "
        "account -- a reset link must stop working once it has been used")
    ctx.log(page.inner_text("#reset-invalid-why"))

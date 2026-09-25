"""Page objects for Acme Freight, the hub's built-in demo site (/demo/freight/).

The pattern teams bring from their own repos: one object per site, methods
named after what a user does, assertions left to the tests. Locators prefer
ids the site keeps stable across releases and what a user sees (labels,
roles) -- which is why the same tests survive the 2.0.0 redesign that
changes the look of every page.
"""
import re

PASSWORD_SECRET = "demo_password"      # Settings -> Credentials, optional
DEFAULT_PASSWORD = "demo-password"      # the demo's published password


def money(text):
    """'$1,234.56' -> 1234.56"""
    return float(re.sub(r"[^0-9.\-]", "", text))


class Freight:
    def __init__(self, page, ctx):
        self.page = page
        self.ctx = ctx
        self.root = ctx.base_url.rstrip("/") + "/freight/"

    def url(self, path=""):
        return self.root + path.lstrip("/")

    def open(self, path=""):
        """Go to a page of the site -- and say so plainly when the site is
        not answering properly (an outage page also has a title and runs
        JavaScript, so without this the test fails 30 s later on a
        missing element, with a message about the element)."""
        url = self.url(path)
        response = self.page.goto(url)
        if response is not None and response.status >= 400:
            title = self.page.title()
            raise AssertionError(f"{url} answered HTTP {response.status} ({title!r}) -- "
                                 f"the site itself is failing, not this test's steps")
        return self

    @property
    def version(self):
        """The release the site says it is (a <meta name="app-version">)."""
        return self.page.get_attribute('meta[name="app-version"]', "content")

    # -- public side -----------------------------------------------------
    def track(self, shipment_id):
        self.open("track/")
        self.page.fill("#track-id", shipment_id)
        self.page.click("#track-btn")
        self.page.wait_for_selector("#track-result, #track-not-found")
        return self

    # -- staff portal ----------------------------------------------------
    def sign_in(self, user="tester"):
        self.open("login/")
        self.page.fill("#username", user)
        self.page.fill("#password", self.ctx.secret(PASSWORD_SECRET, DEFAULT_PASSWORD))
        self.page.click("#login-btn")
        self.page.wait_for_selector("#signed-in-user")
        self.ctx.log(f"signed in as {user}")
        return self

    def sign_out(self):
        self.page.click("#sign-out")
        self.page.wait_for_selector("#login-btn")
        return self

    def wait_for_job(self, timeout_s):
        """Wait for a background job (optimizer, import, report) to finish.

        The job page polls the server and reloads itself when the job ends;
        one wait covers the whole thing, and while it waits the harness
        treats the page as idle -- so a long job does not bloat the reel.
        Returns the final state: done, failed or cancelled."""
        self.page.wait_for_selector("#job-result, #job-failed", timeout=timeout_s * 1000)
        return self.page.get_attribute("#job", "data-state")

    def download(self, selector, save_as):
        """Click a download link and keep the file with the run's artifacts."""
        with self.page.expect_download() as info:
            self.page.click(selector)
        info.value.save_as(str(save_as))
        self.ctx.log(f"downloaded {info.value.suggested_filename}")
        return save_as

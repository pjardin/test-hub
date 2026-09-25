"""Page objects for the built-in demo target.

The pattern teams bring from their own repos: a class per page, methods for
the interactions, assertions kept in the test. Works unchanged here — the
only Test Hub requirement is that the TEST FILE exposes run(page, ctx).
"""


class DemoPage:
    """The hub's own /demo/ page, wrapped the way a real app page would be."""

    def __init__(self, page, ctx):
        self.page = page
        self.ctx = ctx

    def open(self):
        self.page.goto(self.ctx.base_url)
        self.ctx.log(f"opened {self.page.url}")
        return self

    def title(self):
        return self.page.title()

    def submit_request(self, name, email, priority):
        self.page.fill("#name", name)
        self.page.fill("#email", email)
        self.page.select_option("#priority", priority)
        self.page.click("#submit-btn")
        self.page.wait_for_selector("#result", timeout=15000)
        return self

    def result_text(self):
        return self.page.inner_text("#result")

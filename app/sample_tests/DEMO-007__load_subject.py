"""A short, realistic pass — the kind of test you put UNDER load.

A load test repeats one test many times, so the test itself wants to be
short and to exercise something worth measuring. This one does what a user
does: fill a form, submit it, wait for the answer, check the answer is right.

Each `ctx.timed()` block below becomes its own row on the load run's "which
step got slow" table, which is the point: under load you usually find that
one step degrades and the rest are fine.
"""


def run(page, ctx):
    with ctx.timed("open the page"):
        page.goto(ctx.base_url)

    with ctx.timed("fill the form"):
        page.fill("#name", "Load Test User")
        page.fill("#email", "load@example.gov")
        page.select_option("#priority", "high")

    with ctx.timed("submit and wait for the result"):
        page.click("#submit-btn")
        page.wait_for_selector("#result")

    with ctx.timed("check the result"):
        assert "Load Test User" in page.inner_text("#result")

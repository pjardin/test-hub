"""UI work, a long idle wait, then more work.

This is the pattern the ACTIVITY REPLAY exists for: frames are captured
while the form is being filled and while results appear, but the idle wait
in the middle produces (almost) no frames -- so the replay jumps straight
past it with a "skipped idle" marker instead of an hour of nothing.
"""


def run(page, ctx):
    page.goto(ctx.base_url)

    ctx.log("phase 1: doing UI work")
    page.fill("#name", "Slow Backend")
    page.fill("#email", "wait@example.gov")
    page.select_option("#priority", "low")
    ctx.screenshot("work-done")

    ctx.log("phase 2: waiting for the 'slow backend' (idle, ~20s)")
    with ctx.timed("backend wait"):          # shows up in Step timings
        page.wait_for_timeout(20000)         # your real tests might wait an hour here

    ctx.log("phase 3: results arrive, more UI work")
    page.click("#submit-btn")
    page.wait_for_selector("#result")
    ctx.screenshot("results")
    assert "Slow Backend" in page.inner_text("#result")

"""Wait for a long-running job, without recording the wait.

This is the pattern for "kick something off, then wait 30 minutes for it":
a nightly batch, a report build, a slow backend. The demo page's job shows
a countdown that repaints EVERY SECOND on purpose -- the hard case, because
content-hash de-duplication cannot collapse a clock (every tick genuinely
differs).

Two things to notice in the artifacts afterwards:

  * the run took the full wait, but the ACTIVITY REEL only holds the busy
    parts -- starting the job and checking the result. The idle stretch is
    sampled sparsely and the replay jumps it with a "skipped idle" marker.
  * the per-step timings show the wait as one labelled block, so you can
    compare "how long did the job take" across runs on the trend chart.

Change JOB_SECONDS to 1800 for a real 30-minute rehearsal (and raise
timeout_seconds in the sidecar to match, plus a bit).
"""

JOB_SECONDS = 120


def run(page, ctx):
    page.goto(f"{ctx.base_url}?timer={JOB_SECONDS}")

    ctx.log("phase 1: start the job (this part is worth watching)")
    page.click("#timer-btn")
    page.wait_for_selector("#timer-panel:not(.hidden)")
    ctx.screenshot("job-started")

    ctx.log(f"phase 2: waiting up to {JOB_SECONDS}s for it to finish")
    with ctx.timed("job wait"):
        # One wait, not a polling loop: Playwright re-checks for us, and the
        # harness treats an unattended page as idle for activity capture.
        page.wait_for_selector("#timer-done:not(.hidden)",
                               timeout=(JOB_SECONDS + 60) * 1000)

    ctx.log("phase 3: the job finished -- check the outcome")
    ctx.screenshot("job-finished")
    assert "finished" in page.inner_text("#timer-done").lower()
    assert page.is_hidden("#timer-panel"), "the countdown should be gone"

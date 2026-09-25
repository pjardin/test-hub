"""Start a slow server-side job, wait for it, check the answer, keep the file.

The route optimizer plans delivery routes for a fleet of trucks: a real
computation that takes half a minute or so, with live progress on the page.
The test starts it, waits for it in ONE wait (the page polls the server and
reloads itself when the job ends), then checks the result makes sense and
downloads the plan as CSV into this run's artifacts.

The wait is wrapped in ctx.timed("route optimization"), so the hub charts how
long the optimizer takes, run after run and release after release -- which
is exactly how a release that quietly makes it twice as slow gets noticed.
"""
import csv

from _lib.freight import Freight

STOPS, TRUCKS = 25, 4


def run(page, ctx):
    site = Freight(page, ctx).sign_in()
    page.goto(site.url("ops/optimizer/"))
    page.select_option("#region", "california")
    page.fill("#stops", str(STOPS))
    page.fill("#trucks", str(TRUCKS))
    page.click("#start-optimizer")
    page.wait_for_selector("#job-progress")
    ctx.screenshot("optimizer-running")

    with ctx.timed("route optimization"):
        state = site.wait_for_job(timeout_s=170)
    assert state == "done", f"the optimization ended '{state}'"

    saving = float(page.get_attribute("#plan-saving", "data-pct"))
    ctx.log(f"optimized plan: {page.inner_text('#plan-best')} ({saving}% shorter than naive)")
    assert saving >= 25, f"the plan should be far shorter than the naive one; it saved only {saving}%"
    used = int(page.inner_text("#plan-trucks").split()[0])
    drawn = page.locator("svg polyline.route").count()
    assert drawn == used, f"{used} trucks are used but the map draws {drawn} routes"
    ctx.screenshot("route-plan")

    plan = site.download("#download-result", ctx.artifacts_dir / "route-plan.csv")
    with open(plan, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == STOPS, f"the CSV plan should list {STOPS} stops, it lists {len(rows)}"

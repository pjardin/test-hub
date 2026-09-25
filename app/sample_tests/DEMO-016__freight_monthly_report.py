"""Role check, then a LONG wait: the full monthly report takes minutes.

First the access rule: a dispatcher asking for reports gets a polite 403;
reports are for managers. Then, as a manager, the test generates the full
report and waits for it -- about two minutes. That is the case the hub's
activity reel is built for: only the busy moments are kept, the long quiet
middle is sampled sparsely, and video is off for this test (see its
settings), so a long wait stays cheap to record.
"""
import csv

from _lib.freight import Freight, money


def run(page, ctx):
    site = Freight(page, ctx).sign_in("tester")
    page.goto(site.url("ops/reports/"))
    assert page.is_visible("#forbidden"), "a dispatcher must not see monthly reports"
    ctx.log("dispatcher correctly refused; switching to a manager")
    site.sign_out()

    site.sign_in("manager")
    page.goto(site.url("ops/reports/"))
    page.check("input[name=detail][value=full]")
    page.click("#generate-report")
    page.wait_for_selector("#job-progress")
    ctx.screenshot("report-started")

    with ctx.timed("monthly report (full)"):
        state = site.wait_for_job(timeout_s=330)
    assert state == "done", f"the report ended '{state}'"

    shipments = int(page.inner_text("#report-shipments"))
    revenue = money(page.inner_text("#report-revenue"))
    ctx.log(f"{shipments} shipments, revenue ${revenue:,.2f}")
    ctx.screenshot("report")
    assert shipments > 0 and revenue > 0, "an empty report for the current month"

    report = site.download("#download-result", ctx.artifacts_dir / "report.csv")
    with open(report, newline="") as fh:
        kpis = {row["key"]: row for row in csv.DictReader(fh) if row["section"] == "kpi"}
    assert int(kpis["shipments"]["shipments"]) == shipments, "the CSV disagrees with the page"

"""Accessibility (WCAG 2.1 AA -- what Section 508 points to), on every release.

Scans the pages most visitors use with axe-core, the engine behind most
commercial accessibility scanners, shipped in _lib/axe/ so it runs offline.
Fails on the problems a screen-reader or keyboard user actually runs into
(axe's "critical" and "serious"), grouped by rule with the pages they are on;
the full report -- every element, the HTML it quotes, how to fix it -- is
saved as a11y-report.html with the run's files.

Acme Freight 2.0.0's redesign breaks four rules at once (the page language,
a skip link hidden from screen readers, the map's text alternative, brand
colours with too little contrast). A visual check sees none of them: the
pages look fine. That is the point of running both.
"""
from _lib.a11y import Audit
from _lib.freight import Freight

PAGES = [("home page", ""), ("tracking", "track/?id=AF-100001"),
         ("quote, step 1", "quote/?restart=1"), ("staff sign-in", "login/")]


def run(page, ctx):
    site = Freight(page, ctx)
    audit = Audit(page, ctx)
    for label, path in PAGES:
        with ctx.timed(f"scan: {label}"):
            site.open(path)
            page.wait_for_load_state("networkidle")
            audit.scan(label)
    site.sign_in()
    with ctx.timed("scan: staff dashboard"):
        site.open("ops/")
        page.wait_for_load_state("networkidle")
        audit.scan("staff dashboard")
    report = audit.save_report()
    ctx.log(f"report saved: {report.name}")
    audit.assert_clean()

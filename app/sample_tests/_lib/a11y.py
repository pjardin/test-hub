"""Accessibility checks with axe-core, offline.

axe-core -- the engine most commercial accessibility scanners are built on --
ships beside this file (_lib/axe/), so the check works on a network with no
internet. It tests against WCAG 2.1 levels A and AA, the standard that
Section 508 and EN 301 549 point to.

A scan finds what a machine can find: missing text alternatives and labels,
contrast, page language, keyboard traps, ARIA misuse. That is roughly a
third of WCAG; it does not replace a person with a screen reader. What it
adds is that it never forgets to look, on every page, on every release.

    from _lib.a11y import Audit

    audit = Audit(page, ctx)
    audit.scan("home page")        # the page as it is now
    ...                            # navigate, scan again
    audit.save_report()            # a11y-report.html + .json, kept with the run
    audit.assert_clean()           # fails on critical and serious findings
"""
import datetime as dt
import html
import json
from collections import OrderedDict
from pathlib import Path

AXE = Path(__file__).resolve().parent / "axe" / "axe.min.js"
WCAG_21_AA = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa")
BLOCKING = ("critical", "serious")
IMPACT_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}

_RUN = """async (tags) => {
  const r = await axe.run(document, {runOnly: {type: 'tag', values: tags},
                                     resultTypes: ['violations']});
  return {axe: axe.version, violations: r.violations.map(v => ({
    id: v.id, impact: v.impact || 'minor', help: v.help, description: v.description,
    count: v.nodes.length,
    nodes: v.nodes.slice(0, 8).map(n => ({
      target: n.target.join(' '), html: (n.html || '').slice(0, 240),
      summary: (n.failureSummary || '').slice(0, 400)}))}))};
}"""


class Audit:
    def __init__(self, page, ctx, tags=WCAG_21_AA):
        self.page, self.ctx, self.tags = page, ctx, list(tags)
        self.pages = []
        self.axe_version = ""
        self._source = None

    def scan(self, label):
        """Scan the page as it is now. Returns this page's violations."""
        if not self.page.evaluate("() => typeof window.axe === 'object'"):
            if self._source is None:
                self._source = AXE.read_text(encoding="utf-8")
            # evaluate, not add_script_tag: a site whose Content-Security-
            # Policy says script-src 'self' blocks an injected <script> tag,
            # while code evaluated through the browser's debugging protocol is
            # not subject to the page's CSP. (";0": the script's own value is
            # not something Playwright can hand back.)
            self.page.evaluate(self._source + "\n;0")
        result = self.page.evaluate(_RUN, self.tags)
        self.axe_version = result["axe"]
        found = sorted(result["violations"], key=lambda v: IMPACT_ORDER.get(v["impact"], 9))
        self.pages.append({"label": label, "url": self.page.url, "violations": found})
        summary = ", ".join(f"{v['id']} ({v['impact']}, {v['count']})" for v in found)
        self.ctx.log(f"accessibility, {label}: {summary or 'no violations'}")
        return found

    def rules(self, impacts=BLOCKING):
        """The violated rules across every scanned page, worst first:
        {rule id: {help, impact, count, pages}}."""
        rules = OrderedDict()
        for p in self.pages:
            for v in p["violations"]:
                if v["impact"] not in impacts:
                    continue
                r = rules.setdefault(v["id"], {"help": v["help"], "impact": v["impact"],
                                               "count": 0, "pages": []})
                r["count"] += v["count"]
                r["pages"].append(p["label"])
        return OrderedDict(sorted(rules.items(), key=lambda kv: (
            IMPACT_ORDER.get(kv[1]["impact"], 9), -kv[1]["count"])))

    def assert_clean(self, show=4):
        """Fail listing the broken rules, worst first. Kept short on purpose:
        the hub shows the first 6 lines / 500 characters of a failure, and
        the report has every element anyway."""
        rules = self.rules()
        if not rules:
            return
        everywhere = len(self.pages)
        shown = list(rules.items())[:show]

        def where(r):
            pages = sorted(set(r["pages"]), key=r["pages"].index)
            if len(pages) == everywhere > 1:
                return "every page"
            return ", ".join(pages) if len(pages) <= 2 else f"{len(pages)} pages"

        head = f"WCAG 2.1 AA: {len(rules)} rule(s) broken (details: a11y-report.html)"
        more = [f"...and {len(rules) - show} more"] if len(rules) > show else []
        full = [f"{r['help']} [{rid}]: {r['count']} on {where(r)}" for rid, r in shown]
        message = head + "\n  - " + "\n  - ".join(full + more)
        if len(message) > 470:          # the hub keeps 500, "AssertionError: " included
            compact = [f"{rid}: {r['count']} on {where(r)}" for rid, r in shown]
            message = head + "\n  - " + "\n  - ".join(compact + more)
        raise AssertionError(message)

    def save_report(self, name="a11y-report"):
        """Write <name>.json and a self-contained <name>.html next to the run's
        other files. Everything quoted from the page is escaped: the report
        repeats the site's HTML, and must never run it."""
        folder = self.ctx.artifacts_dir
        data = {"standard": "WCAG 2.1 A/AA", "tags": self.tags, "axe": self.axe_version,
                "pages": self.pages}
        (folder / f"{name}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        path = folder / f"{name}.html"
        path.write_text(self._html(), encoding="utf-8")
        return path

    def _html(self):
        e = html.escape
        total = sum(len(p["violations"]) for p in self.pages)
        out = [
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
            "<title>Accessibility report</title><style>",
            "body{font:15px/1.5 system-ui,sans-serif;margin:24px;color:#101828;max-width:1100px}",
            "h1{margin:0 0 4px}h2{margin:28px 0 8px;border-bottom:1px solid #d0d5dd}",
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #d0d5dd;padding:6px 8px;"
            "text-align:left;vertical-align:top}th{background:#f2f4f7}",
            ".critical,.serious{color:#b42318;font-weight:700}.moderate{color:#93370d}"
            ".minor{color:#475467}.ok{color:#067647;font-weight:700}",
            "code,pre{font:12px ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}",
            "pre{background:#f2f4f7;padding:6px;margin:4px 0}.muted{color:#475467}",
            "</style></head><body>",
            "<h1>Accessibility report</h1>",
            f"<p class='muted'>WCAG 2.1 levels A and AA &middot; axe-core {e(self.axe_version)} "
            f"&middot; {e(dt.datetime.now().strftime('%Y-%m-%d %H:%M'))} &middot; "
            f"{len(self.pages)} page(s), {total} violated rule(s) in all</p>",
            "<table><tr><th>Page</th><th>Address</th><th>Violations</th></tr>",
        ]
        for p in self.pages:
            n = len(p["violations"])
            out.append(f"<tr><td>{e(p['label'])}</td><td><code>{e(p['url'])}</code></td>"
                       f"<td class='{'serious' if n else 'ok'}'>{n or 'none'}</td></tr>")
        out.append("</table>")
        for p in self.pages:
            if not p["violations"]:
                continue
            out.append(f"<h2>{e(p['label'])}</h2>")
            for v in p["violations"]:
                out.append(f"<h3><span class='{e(v['impact'])}'>{e(v['impact'])}</span> "
                           f"{e(v['help'])} <code>{e(v['id'])}</code></h3>")
                out.append(f"<p>{e(v['description'])} &middot; {v['count']} element(s)"
                           f"{' (first 8 shown)' if v['count'] > 8 else ''}</p>")
                for node in v["nodes"]:
                    out.append(f"<p class='muted'><code>{e(node['target'])}</code></p>"
                               f"<pre>{e(node['html'])}</pre>"
                               f"<pre class='muted'>{e(node['summary'])}</pre>")
        out.append("<p class='muted'>A machine finds about a third of WCAG problems; this report "
                   "does not replace a review with a screen reader and a keyboard.</p>")
        out.append("</body></html>")
        return "\n".join(out)

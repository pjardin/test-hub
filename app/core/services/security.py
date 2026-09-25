"""Aggregating the security observations that runs collect.

Each run stores what the browser saw (harness/security.py). This turns a pile
of per-run reports into "what is wrong with the application right now", which
is the only form anyone can act on.

Deliberately built from the LATEST regular run of each test rather than every
run ever: a finding fixed last week should stop being reported, and a list
that mixes current state with history is a list nobody reads.
"""
import json
from collections import defaultdict

from core.models import Run, Test

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def latest_reports():
    """The newest security report per test. Returns [(test, report, run)]."""
    out = []
    for test in Test.objects.filter(archived=False):
        run = (Run.regular().filter(test=test, status__in=Run.FINISHED_STATUSES)
               .exclude(security_json="").exclude(security_json="{}")
               .order_by("-queued_at").first())
        if run is None:
            continue
        report = run.security
        if report.get("findings") is not None:
            out.append((test, report, run))
    return out


def posture() -> dict:
    """Current state of the application, as seen from every test's last run.

    Findings are grouped by KIND, not listed per run: "no CSP header" seen by
    nine tests is one problem with one fix, and nine copies of it buries the
    thing that only one test noticed.
    """
    grouped = defaultdict(lambda: {"tests": [], "example": None, "count": 0})
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    reports = latest_reports()

    for test, report, run in reports:
        for finding in report.get("findings", []):
            key = (finding.get("kind", "other"), finding.get("title", ""))
            entry = grouped[key]
            entry["count"] += 1
            if test.test_id not in entry["tests"]:
                entry["tests"].append(test.test_id)
            if entry["example"] is None:
                entry["example"] = dict(finding, run_id=run.pk)

    issues = []
    for (kind, title), entry in grouped.items():
        example = entry["example"] or {}
        issues.append({
            "kind": kind,
            "title": title,
            "severity": example.get("severity", "info"),
            "detail": example.get("detail", ""),
            "fix": example.get("fix", ""),
            "tests": sorted(entry["tests"]),
            "test_count": len(entry["tests"]),
            "run_id": example.get("run_id"),
        })
    for issue in issues:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    issues.sort(key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9),
                               -i["test_count"], i["title"]))

    external = {}
    scheme = ""
    for _test, report, _run in reports:
        observed = report.get("observed", {})
        scheme = scheme or observed.get("scheme", "")
        for host, n in (observed.get("external_hosts") or {}).items():
            external[host] = external.get(host, 0) + n

    return {
        "issues": issues,
        "counts": counts,
        "tests_reporting": len(reports),
        "external_hosts": sorted(external.items(), key=lambda kv: -kv[1]),
        "scheme": scheme,
        "worst": ("high" if counts["high"] else
                  "medium" if counts["medium"] else
                  "low" if counts["low"] else "clean"),
    }


def run_findings(run: Run) -> list:
    """One run's findings, worst first."""
    findings = run.security.get("findings", [])
    return sorted(findings,
                  key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))

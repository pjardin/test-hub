"""Analytics across WEBSITE VERSIONS — which version of the site under test
made which tests slower, flakier, or broken.

Metrics answers "how are my tests doing?"; this answers "what did each
version of the WEBSITE do to them?". Every run is stamped with
target.version from config.json at the moment it ran, so the history
already contains the answer — this module just pivots it.

Everything here excludes load iterations (Run.regular()): one load run
would otherwise drown a version in a thousand identical rows, the exact
bug class fixed in v2.6. Pass rates count finished runs; duration medians
use only representative runs (passed/failed) so a killed run's 3 seconds
does not make a version look fast.

The CSV sections exist for a specific field reality: the lab's chat tool
can carry TEXT between networks but not files or images. So every table
is also available as copyable CSV text — a tester pastes it out, and a
spreadsheet on the other side does the rest.
"""
import csv
import io

from django.db.models import Count, Q

from core import appconfig
from core.models import Run
from core.services.stats import percentile

UNVERSIONED = "(not recorded)"


def _median(values):
    return percentile(values, 50)


def _regular():
    return Run.regular().exclude(status__in=(Run.QUEUED, Run.RUNNING))


def _label(version):
    return version or UNVERSIONED


def version_order():
    """Website versions in the order they FIRST appeared in run history —
    config.json's version strings are free text, so first-seen beats any
    attempt to parse them."""
    seen = []
    rows = (_regular().order_by("queued_at")
            .values_list("target_version", flat=True))
    for v in rows:
        v = _label(v)
        if v not in seen:
            seen.append(v)
    return seen


def versions_summary():
    """One row per website version: volume, outcomes, pass rate, speed."""
    out = []
    for version in version_order():
        runs = _regular().filter(target_version="" if version == UNVERSIONED
                                 else version)
        agg = runs.aggregate(
            total=Count("id"),
            passed=Count("id", filter=Q(status=Run.PASSED)),
            failed=Count("id", filter=Q(status=Run.FAILED)),
        )
        durations = list(
            runs.filter(status__in=Run.REPRESENTATIVE_STATUSES,
                        duration_seconds__isnull=False)
            .values_list("duration_seconds", flat=True))
        finished = agg["total"]
        first = runs.order_by("queued_at").first()
        last = runs.order_by("-queued_at").first()
        out.append({
            "version": version,
            "runs": finished,
            "passed": agg["passed"],
            "failed": agg["failed"],
            "other": finished - agg["passed"] - agg["failed"],
            "pass_rate": round(100.0 * agg["passed"] / finished, 1) if finished else None,
            "median_s": round(_median(durations), 2) if durations else None,
            "tests": runs.values("test_id").distinct().count(),
            "first_run": first.queued_at if first else None,
            "last_run": last.queued_at if last else None,
        })
    return out


def per_test_by_version():
    """{test: {version: cell}} — the matrix. Cells carry runs / pass rate /
    median so a version that broke or slowed ONE test is visible at a
    glance rather than averaged away."""
    versions = version_order()
    matrix = {}
    rows = (_regular().select_related("test").order_by("test__test_id", "queued_at"))
    for run in rows:
        cell = (matrix.setdefault(run.test, {})
                .setdefault(_label(run.target_version),
                            {"runs": 0, "passed": 0, "failed": 0, "durations": []}))
        cell["runs"] += 1
        if run.status == Run.PASSED:
            cell["passed"] += 1
        elif run.status == Run.FAILED:
            cell["failed"] += 1
        if (run.status in Run.REPRESENTATIVE_STATUSES
                and run.duration_seconds is not None):
            cell["durations"].append(run.duration_seconds)
    for cells in matrix.values():
        for cell in cells.values():
            n = cell["runs"]
            cell["pass_rate"] = round(100.0 * cell["passed"] / n, 1) if n else None
            cell["median_s"] = (round(_median(cell["durations"]), 2)
                                if cell["durations"] else None)
            del cell["durations"]
    return versions, matrix


def speed_trend():
    """Per test: the CURRENT version's median against the version before it.
    'The site got slower' hides inside averages; per-test deltas name the
    page that did it."""
    versions, matrix = per_test_by_version()
    if len(versions) < 2:
        return versions, []
    current, previous = versions[-1], versions[-2]
    rows = []
    for test, cells in sorted(matrix.items(), key=lambda kv: kv[0].test_id):
        cur, prev = cells.get(current), cells.get(previous)
        if not cur or not prev or cur["median_s"] is None or prev["median_s"] is None:
            continue
        delta = round(cur["median_s"] - prev["median_s"], 2)
        pct = round(100.0 * delta / prev["median_s"], 1) if prev["median_s"] else None
        rows.append({
            "test": test,
            "current_median": cur["median_s"],
            "previous_median": prev["median_s"],
            "delta_s": delta,
            "delta_pct": pct,
            "direction": ("slower" if delta > 0.05 else
                          "faster" if delta < -0.05 else "same"),
        })
    rows.sort(key=lambda r: r["delta_s"], reverse=True)
    return [previous, current], rows


# ---------------------------------------------------------------------------
# CSV text sections — pasteable, never a file
# ---------------------------------------------------------------------------

def _csv(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def csv_sections(recent_limit=200):
    """Every analytics table as CSV text. Each section says what it is and
    suggests a filename for whoever saves the paste on the other side."""
    cfg = appconfig.get_config()
    current = _label(cfg.target_version)
    sections = []

    summary = versions_summary()
    sections.append({
        "slug": "versions",
        "title": "Website versions — summary",
        "filename": "website_versions.csv",
        "note": "One row per version of the website ever tested, oldest first.",
        "text": _csv(
            ["website_version", "tests", "runs", "passed", "failed", "other",
             "pass_rate_pct", "median_duration_s", "first_run", "last_run"],
            [[s["version"], s["tests"], s["runs"], s["passed"], s["failed"],
              s["other"], s["pass_rate"], s["median_s"],
              _dt(s["first_run"]), _dt(s["last_run"])] for s in summary]),
    })

    versions, matrix = per_test_by_version()
    cur_rows = []
    for test, cells in sorted(matrix.items(), key=lambda kv: kv[0].test_id):
        cell = cells.get(current)
        if not cell:
            continue
        last = (_regular().filter(test=test,
                                  target_version="" if current == UNVERSIONED
                                  else current)
                .order_by("-queued_at").first())
        cur_rows.append([test.test_id, test.name,
                         last.status if last else "",
                         cell["runs"], cell["passed"], cell["failed"],
                         cell["pass_rate"], cell["median_s"]])
    sections.append({
        "slug": "current",
        "title": f"Current website version ({current}) — per test",
        "filename": "current_version_tests.csv",
        "note": "Each test against the version being tested right now: its "
                "latest result and its totals on this version.",
        "text": _csv(
            ["test_id", "test_name", "latest_result", "runs", "passed",
             "failed", "pass_rate_pct", "median_duration_s"], cur_rows),
    })

    long_rows = []
    for test, cells in sorted(matrix.items(), key=lambda kv: kv[0].test_id):
        for version in versions:
            cell = cells.get(version)
            if cell:
                long_rows.append([version, test.test_id, test.name,
                                  cell["runs"], cell["passed"], cell["failed"],
                                  cell["pass_rate"], cell["median_s"]])
    sections.append({
        "slug": "matrix",
        "title": "Every test × every website version",
        "filename": "tests_by_version.csv",
        "note": "Long form, ready to pivot in a spreadsheet: one row per "
                "test per version.",
        "text": _csv(
            ["website_version", "test_id", "test_name", "runs", "passed",
             "failed", "pass_rate_pct", "median_duration_s"], long_rows),
    })

    pair, trend = speed_trend()
    sections.append({
        "slug": "trend",
        "title": "Speed: current version vs the one before",
        "filename": "speed_trend.csv",
        "note": ("Positive delta = the test got SLOWER on the newer version. "
                 + (f"Comparing {pair[1]} against {pair[0]}." if trend else
                    "Needs runs on at least two website versions.")),
        "text": _csv(
            ["test_id", "test_name", "previous_median_s", "current_median_s",
             "delta_s", "delta_pct", "direction"],
            [[r["test"].test_id, r["test"].name, r["previous_median"],
              r["current_median"], r["delta_s"], r["delta_pct"],
              r["direction"]] for r in trend]),
    })

    recent = (_regular().select_related("test")
              .order_by("-queued_at")[:recent_limit])
    sections.append({
        "slug": "recent",
        "title": f"Most recent {recent_limit} runs — raw detail",
        "filename": "recent_runs.csv",
        "note": "Newest first. Every row names the test and whether it "
                "passed or failed.",
        "text": _csv(
            ["run_id", "test_id", "test_name", "result", "website_version",
             "started", "duration_s"],
            [[r.pk, r.test.test_id, r.test.name, r.status,
              _label(r.target_version), _dt(r.started_at),
              r.duration_seconds] for r in recent]),
    })
    return sections

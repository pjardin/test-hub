"""Metrics over run history: ETAs, pass rates, flakiness, trends.

Everything here is computed from the Run table in plain Python. At this
tool's scale (hundreds of tests, tens of thousands of runs) that is instant,
and it keeps every query portable across SQLite builds.
"""
import re
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from core.models import Run, Test

ETA_SAMPLE = 10  # how many recent representative runs feed the estimate


def percentile(values, pct):
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def estimate_seconds(test) -> "float | None":
    """Average of the last N passed/failed durations. None when there is not
    enough history -- the UI shows that as 'no estimate' rather than a guess."""
    durations = list(
        Run.regular().filter(test=test, status__in=Run.REPRESENTATIVE_STATUSES,
                           duration_seconds__isnull=False)
        .order_by("-finished_at")
        .values_list("duration_seconds", flat=True)[:ETA_SAMPLE]
    )
    if not durations:
        return None
    return sum(durations) / len(durations)


def eta_for_run(run, estimate=None) -> "float | None":
    if estimate is None:
        estimate = estimate_seconds(run.test)
    if estimate is None:
        return None
    if run.status == Run.QUEUED:
        return estimate
    if run.status == Run.RUNNING and run.started_at:
        elapsed = (timezone.now() - run.started_at).total_seconds()
        return max(0.0, estimate - elapsed)
    return None


def flakiness(statuses) -> "float | None":
    """Share of adjacent pass<->fail flips in the recent history (0 = stable,
    1 = alternates every run). Only pass/fail runs count."""
    seq = [s for s in statuses if s in Run.REPRESENTATIVE_STATUSES]
    if len(seq) < 2:
        return None
    flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    return flips / (len(seq) - 1)


def test_stats(test) -> dict:
    finished = list(
        Run.regular().filter(test=test, status__in=Run.FINISHED_STATUSES)
        .order_by("-queued_at")
        .values("status", "duration_seconds")[:500]
    )
    total = len(finished)
    passed = sum(1 for r in finished if r["status"] == Run.PASSED)
    failed = sum(1 for r in finished if r["status"] == Run.FAILED)
    durations = [r["duration_seconds"] for r in finished
                 if r["status"] in Run.REPRESENTATIVE_STATUSES
                 and r["duration_seconds"] is not None]
    judged = passed + failed

    streak = 0
    streak_status = None
    for r in finished:
        if r["status"] not in Run.REPRESENTATIVE_STATUSES:
            continue
        if streak_status is None:
            streak_status = r["status"]
        if r["status"] != streak_status:
            break
        streak += 1

    return {
        "total_runs": total,
        "passed": passed,
        "failed": failed,
        "other": total - judged,
        "pass_rate": (passed / judged * 100) if judged else None,
        "avg_duration": (sum(durations) / len(durations)) if durations else None,
        "p50_duration": percentile(durations, 50),
        "p95_duration": percentile(durations, 95),
        "flakiness": flakiness([r["status"] for r in finished][:20][::-1]),
        "estimate": estimate_seconds(test),
        "streak": streak,
        "streak_status": streak_status,
    }


def history_series(test, limit=200) -> list:
    """Chart-ready history, oldest first: one point per finished run."""
    runs = list(
        Run.regular().filter(test=test, status__in=Run.FINISHED_STATUSES)
        .order_by("-queued_at")[:limit]
    )
    runs.reverse()
    return [
        {
            "id": r.pk,
            "when": timezone.localtime(r.finished_at or r.queued_at).strftime("%Y-%m-%d %H:%M"),
            "status": r.status,
            "duration": round(r.duration_seconds, 2) if r.duration_seconds is not None else None,
            "version": r.target_version or "?",
            "trigger": r.trigger,
            "browser": r.browser,
        }
        for r in runs
    ]


def version_breakdown(test) -> list:
    """Outcome counts per version of the app under test."""
    buckets = defaultdict(lambda: {"passed": 0, "failed": 0, "other": 0, "durations": []})
    rows = Run.regular().filter(test=test, status__in=Run.FINISHED_STATUSES) \
                      .values("target_version", "status", "duration_seconds")
    for row in rows:
        b = buckets[row["target_version"] or "?"]
        if row["status"] == Run.PASSED:
            b["passed"] += 1
        elif row["status"] == Run.FAILED:
            b["failed"] += 1
        else:
            b["other"] += 1
        if row["status"] in Run.REPRESENTATIVE_STATUSES and row["duration_seconds"]:
            b["durations"].append(row["duration_seconds"])
    out = []
    for version, b in buckets.items():
        judged = b["passed"] + b["failed"]
        out.append({
            "version": version,
            "passed": b["passed"],
            "failed": b["failed"],
            "other": b["other"],
            "pass_rate": (b["passed"] / judged * 100) if judged else None,
            "avg_duration": (sum(b["durations"]) / len(b["durations"])) if b["durations"] else None,
        })
    out.sort(key=lambda x: x["version"])
    return out


# ---------------------------------------------------------------------------
# global dashboards
# ---------------------------------------------------------------------------

def timing_trends(test, limit_runs=30, top=6) -> dict:
    """Per-step durations across the last N finished runs, matched by step
    NAME (e.g. 'click #submit-btn' or a ctx.timed label). Feeds the
    'step timing trends' chart: one line per step, one point per run."""
    runs = list(
        Run.regular().filter(test=test, status__in=Run.FINISHED_STATUSES)
        .exclude(timings_json="[]").order_by("-queued_at")[:limit_runs]
    )
    runs.reverse()
    if not runs:
        return {"labels": [], "series": []}

    per_run = []   # {name: ms} per run, first occurrence wins
    totals = {}
    counts = {}
    for run in runs:
        by_name = {}
        for entry in run.timings:
            name = entry.get("name")
            ms = entry.get("ms")
            if not name or not isinstance(ms, (int, float)) or name in by_name:
                continue
            by_name[name] = ms
        per_run.append(by_name)
        for name, ms in by_name.items():
            totals[name] = totals.get(name, 0.0) + ms
            counts[name] = counts.get(name, 0) + 1

    min_seen = 2 if len(runs) > 1 else 1
    candidates = [n for n, c in counts.items() if c >= min_seen]
    candidates.sort(key=lambda n: -(totals[n] / counts[n]))
    chosen = candidates[:top]

    labels = [timezone.localtime(r.finished_at or r.queued_at).strftime("%m-%d %H:%M")
              for r in runs]
    series = [{"name": name,
               "data": [round(by_name[name], 1) if name in by_name else None
                        for by_name in per_run]}
              for name in chosen]
    return {"labels": labels, "series": series, "run_ids": [r.pk for r in runs]}


def runs_per_day(days=30) -> list:
    since = timezone.now() - timedelta(days=days)
    buckets = {}
    for r in Run.regular().filter(queued_at__gte=since,
                                status__in=Run.FINISHED_STATUSES) \
                        .values("queued_at", "status"):
        day = timezone.localtime(r["queued_at"]).date().isoformat()
        b = buckets.setdefault(day, {"passed": 0, "failed": 0, "other": 0})
        if r["status"] == Run.PASSED:
            b["passed"] += 1
        elif r["status"] == Run.FAILED:
            b["failed"] += 1
        else:
            b["other"] += 1
    today = timezone.localtime(timezone.now()).date()
    out = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        b = buckets.get(day, {"passed": 0, "failed": 0, "other": 0})
        judged = b["passed"] + b["failed"]
        out.append({"day": day, **b,
                    "pass_rate": (b["passed"] / judged * 100) if judged else None})
    return out


def global_overview(days=30) -> dict:
    since = timezone.now() - timedelta(days=days)
    finished = Run.regular().filter(queued_at__gte=since, status__in=Run.FINISHED_STATUSES)
    total = finished.count()
    passed = finished.filter(status=Run.PASSED).count()
    failed = finished.filter(status=Run.FAILED).count()
    durations = list(finished.filter(status__in=Run.REPRESENTATIVE_STATUSES,
                                     duration_seconds__isnull=False)
                     .values_list("duration_seconds", flat=True))
    judged = passed + failed
    return {
        "window_days": days,
        "tests": Test.objects.filter(archived=False).count(),
        "runs": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / judged * 100) if judged else None,
        "avg_duration": (sum(durations) / len(durations)) if durations else None,
        "p95_duration": percentile(durations, 95),
        "total_test_time": sum(durations) if durations else 0,
    }


def per_test_table(days=90, limit=None) -> list:
    """One row per active test with its key numbers; feeds several charts."""
    since = timezone.now() - timedelta(days=days)
    rows = []
    for test in Test.objects.filter(archived=False):
        runs = list(
            Run.regular().filter(test=test, queued_at__gte=since,
                               status__in=Run.FINISHED_STATUSES)
            .order_by("-queued_at").values("status", "duration_seconds")[:200]
        )
        if not runs:
            rows.append({"test_id": test.test_id, "name": test.display_name,
                         "runs": 0, "pass_rate": None, "avg_duration": None,
                         "p95_duration": None, "flakiness": None,
                         "last_status": None})
            continue
        durations = [r["duration_seconds"] for r in runs
                     if r["status"] in Run.REPRESENTATIVE_STATUSES and r["duration_seconds"]]
        passed = sum(1 for r in runs if r["status"] == Run.PASSED)
        failed = sum(1 for r in runs if r["status"] == Run.FAILED)
        judged = passed + failed
        rows.append({
            "test_id": test.test_id,
            "name": test.display_name,
            "runs": len(runs),
            "pass_rate": (passed / judged * 100) if judged else None,
            "avg_duration": (sum(durations) / len(durations)) if durations else None,
            "p95_duration": percentile(durations, 95),
            "flakiness": flakiness([r["status"] for r in runs][:20][::-1]),
            "last_status": runs[0]["status"],
        })
    rows.sort(key=lambda r: r["test_id"])
    return rows[:limit] if limit else rows


def trigger_split(days=30) -> dict:
    since = timezone.now() - timedelta(days=days)
    out = {}
    for r in Run.regular().filter(queued_at__gte=since).values("trigger"):
        out[r["trigger"] or "manual"] = out.get(r["trigger"] or "manual", 0) + 1
    return out


def version_split(days=90) -> list:
    since = timezone.now() - timedelta(days=days)
    buckets = defaultdict(lambda: {"passed": 0, "failed": 0, "other": 0})
    for r in Run.regular().filter(queued_at__gte=since,
                                status__in=Run.FINISHED_STATUSES) \
                        .values("target_version", "status"):
        b = buckets[r["target_version"] or "?"]
        if r["status"] == Run.PASSED:
            b["passed"] += 1
        elif r["status"] == Run.FAILED:
            b["failed"] += 1
        else:
            b["other"] += 1
    return [{"version": v, **b} for v, b in sorted(buckets.items())]


def failing_now() -> list:
    """Tests whose most recent judged run failed, with the streak length."""
    out = []
    for test in Test.objects.filter(archived=False):
        recent = list(
            Run.regular().filter(test=test, status__in=Run.REPRESENTATIVE_STATUSES)
            .order_by("-queued_at").values_list("status", flat=True)[:50]
        )
        if not recent or recent[0] != Run.FAILED:
            continue
        streak = 0
        for s in recent:
            if s != Run.FAILED:
                break
            streak += 1
        out.append({"test_id": test.test_id, "name": test.display_name, "streak": streak})
    out.sort(key=lambda r: -r["streak"])
    return out


# ---------------------------------------------------------------------------
# failure analysis
# ---------------------------------------------------------------------------

_ERR_NOISE = [
    (re.compile(r"\d+"), "#"),                      # ids, counts, timings
    (re.compile(r"0x[0-9a-f]+", re.I), "#"),
    (re.compile(r"/[^\s'\"]+/"), "<path>/"),
    (re.compile(r"\s+"), " "),
]


def error_signature(message: str) -> str:
    """A stable key for 'the same failure'. Numbers/paths are noise; the
    first line carries the meaning."""
    lines = (message or "").strip().splitlines()
    if not lines:
        return ""
    first = lines[0]
    for pattern, repl in _ERR_NOISE:
        first = pattern.sub(repl, first)
    return first.strip()[:160]


def error_clusters(days=30, test=None, limit=8) -> list:
    """Failures grouped by what actually went wrong. When one app bug breaks
    five tests, this shows ONE row with five tests -- which is the single
    most useful thing to know before triaging."""
    since = timezone.now() - timedelta(days=days)
    runs = Run.regular().filter(queued_at__gte=since,
                              status__in=(Run.FAILED, Run.ERROR, Run.TIMEOUT))
    if test is not None:
        runs = runs.filter(test=test)
    buckets = {}
    for run in runs.select_related("test").order_by("-queued_at")[:1000]:
        sig = error_signature(run.error_message) or f"({run.status}, no message)"
        b = buckets.setdefault(sig, {"signature": sig, "count": 0, "tests": set(),
                                     "last": None, "example_run": None,
                                     "sample": ""})
        b["count"] += 1
        b["tests"].add(run.test.test_id)
        if b["last"] is None:
            b["last"] = run.finished_at or run.queued_at
            b["example_run"] = run.pk
            # A run CAN have an empty message (harness died before writing
            # result.json). splitlines() on "" is [], and the resulting
            # IndexError used to 500 the whole metrics page for 30 days.
            lines = (run.error_message or "").strip().splitlines()
            b["sample"] = lines[0][:200] if lines else f"({run.status}, no message)"
    out = [{**b, "tests": sorted(b["tests"])} for b in buckets.values()]
    out.sort(key=lambda r: (-r["count"], r["signature"]))
    return out[:limit]


def new_vs_chronic(new_within_days=2) -> dict:
    """Split currently-failing tests into 'just started failing' (a fresh
    regression -- look now) and 'chronic' (already known). Passing runs are
    the boundary: the streak is how many judged runs have failed in a row."""
    cutoff = timezone.now() - timedelta(days=new_within_days)
    new, chronic = [], []
    for test in Test.objects.filter(archived=False):
        recent = list(
            Run.regular().filter(test=test, status__in=Run.REPRESENTATIVE_STATUSES)
            .order_by("-queued_at")[:60]
        )
        if not recent or recent[0].status != Run.FAILED:
            continue
        streak = 0
        for run in recent:
            if run.status != Run.FAILED:
                break
            streak += 1
        first_fail = recent[streak - 1] if streak <= len(recent) else recent[-1]
        started = first_fail.queued_at
        row = {"test_id": test.test_id, "name": test.display_name,
               "streak": streak, "since": started,
               "last_run_id": recent[0].pk,
               "error": error_signature(recent[0].error_message)[:120]}
        (new if started >= cutoff else chronic).append(row)
    new.sort(key=lambda r: r["since"], reverse=True)
    chronic.sort(key=lambda r: -r["streak"])
    return {"new": new, "chronic": chronic, "window_days": new_within_days}


# ---------------------------------------------------------------------------
# timing analysis
# ---------------------------------------------------------------------------

def slowest_steps(days=30, top=12) -> list:
    """The slowest INDIVIDUAL steps across the whole suite -- where the app
    (or the waiting) actually spends its time, regardless of which test."""
    since = timezone.now() - timedelta(days=days)
    agg = {}
    for run in (Run.regular().filter(queued_at__gte=since,
                                   status__in=Run.REPRESENTATIVE_STATUSES)
                .exclude(timings_json="[]").select_related("test")
                .order_by("-queued_at")[:400]):
        for entry in run.timings:
            name, ms = entry.get("name"), entry.get("ms")
            if not name or not isinstance(ms, (int, float)):
                continue
            key = (run.test.test_id, name)
            a = agg.setdefault(key, {"test_id": run.test.test_id, "step": name,
                                     "samples": []})
            a["samples"].append(ms)
    rows = []
    for a in agg.values():
        s = a["samples"]
        rows.append({"test_id": a["test_id"], "step": a["step"][:60],
                     "label": f"{a['test_id']} · {a['step'][:40]}",
                     "runs": len(s),
                     "avg_ms": round(sum(s) / len(s), 1),
                     "max_ms": round(max(s), 1),
                     "p95_ms": round(percentile(s, 95) or 0, 1)})
    rows.sort(key=lambda r: -r["avg_ms"])
    return rows[:top]


def duration_histogram(test, buckets=12) -> dict:
    """Distribution of a test's durations. An average hides bimodality
    ('usually 3s, sometimes 40s'); a histogram shows it immediately."""
    values = list(
        Run.regular().filter(test=test, status__in=Run.REPRESENTATIVE_STATUSES,
                           duration_seconds__isnull=False)
        .values_list("duration_seconds", flat=True)[:1000]
    )
    if len(values) < 3:
        return {"labels": [], "counts": []}
    low, high = min(values), max(values)
    if high - low < 1e-6:
        return {"labels": [f"{low:.1f}s"], "counts": [len(values)]}
    width = (high - low) / buckets
    counts = [0] * buckets
    for v in values:
        idx = min(buckets - 1, int((v - low) / width))
        counts[idx] += 1
    labels = [f"{low + i * width:.1f}" for i in range(buckets)]
    return {"labels": labels, "counts": counts,
            "min": round(low, 2), "max": round(high, 2)}


# ---------------------------------------------------------------------------
# when do things break? (weekday x time-of-day)
# ---------------------------------------------------------------------------

HOUR_BUCKETS = [(0, 6, "00-06"), (6, 9, "06-09"), (9, 12, "09-12"),
                (12, 15, "12-15"), (15, 18, "15-18"), (18, 24, "18-24")]


def failure_heatmap(days=60) -> dict:
    """Failures by weekday and time of day, in the hub's local timezone.
    Nightly-deploy or backup-window problems show up as a stripe."""
    since = timezone.now() - timedelta(days=days)
    grid = [[{"runs": 0, "fails": 0} for _ in HOUR_BUCKETS] for _ in range(7)]
    for run in Run.regular().filter(queued_at__gte=since,
                                  status__in=Run.FINISHED_STATUSES) \
                          .values("queued_at", "status"):
        local = timezone.localtime(run["queued_at"])
        col = next((i for i, (lo, hi, _) in enumerate(HOUR_BUCKETS)
                    if lo <= local.hour < hi), len(HOUR_BUCKETS) - 1)
        cell = grid[local.weekday()][col]
        cell["runs"] += 1
        if run["status"] != Run.PASSED:
            cell["fails"] += 1
    worst = max((c["fails"] for row in grid for c in row), default=0)
    rows = []
    for d, row in enumerate(grid):
        cells = []
        for i, cell in enumerate(row):
            intensity = (cell["fails"] / worst) if worst else 0
            cells.append({**cell, "bucket": HOUR_BUCKETS[i][2],
                          "intensity": round(intensity, 3)})
        rows.append({"day": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d],
                     "cells": cells})
    return {"rows": rows, "buckets": [b[2] for b in HOUR_BUCKETS],
            "worst": worst, "days": days}


# ---------------------------------------------------------------------------
# suite health / staleness
# ---------------------------------------------------------------------------

def suite_health(stale_days=14) -> dict:
    """Is the suite itself rotting? Never-run tests and long-untouched ones
    are the usual silent decay."""
    now = timezone.now()
    never, stale, disabled = [], [], []
    for test in Test.objects.filter(archived=False):
        if not test.enabled:
            disabled.append(test.test_id)
        last = Run.regular().filter(test=test).order_by("-queued_at").first()
        if last is None:
            never.append(test.test_id)
        elif (now - last.queued_at).days >= stale_days:
            stale.append({"test_id": test.test_id,
                          "days": (now - last.queued_at).days})
    stale.sort(key=lambda r: -r["days"])
    return {"never_run": never, "stale": stale, "disabled": disabled,
            "stale_days": stale_days,
            "total": Test.objects.filter(archived=False).count()}


# ---------------------------------------------------------------------------
# per-group and per-test small multiples
# ---------------------------------------------------------------------------

def group_trend(group, limit=20) -> dict:
    """Pass rate of a group's batches over time -- the release-readiness
    curve teams actually look at."""
    from core.models import Batch
    batches = list(Batch.objects.filter(group=group).order_by("-created_at")[:limit])
    batches.reverse()
    labels, rates, totals = [], [], []
    for batch in batches:
        counts = batch.runs_summary
        passed = counts.get(Run.PASSED, 0)
        judged = passed + counts.get(Run.FAILED, 0)
        labels.append(timezone.localtime(batch.created_at).strftime("%m-%d %H:%M"))
        rates.append(round(passed / judged * 100, 1) if judged else None)
        totals.append(sum(counts.values()))
    return {"labels": labels, "rates": rates, "totals": totals}


# --------------------------------------------------------------------------
# run history: one line per batch (Groups and Schedules pages), and the
# per-test "usual" the batch page compares against
# --------------------------------------------------------------------------

SOURCE_LABELS = {"group": "Run group button", "schedule": "Schedule",
                 "terminal": "Terminal", "selection": "Selected tests",
                 "manual": "Run button"}
USUAL_SAMPLE = 20      # earlier runs the median is taken over
USUAL_MIN = 3          # fewer than this is no "usual" at all
SLOWER, FASTER = 1.25, 0.80


def batch_source(batch, runs) -> str:
    """How a batch was started, in words. Terminal batches were recorded as
    'manual' before 2.16; their runs still say so (trigger cli)."""
    if batch.trigger == "manual" and (batch.label.startswith("cli:")
                                      or any(r.trigger == "cli" for r in runs)):
        return "Terminal"
    return SOURCE_LABELS.get(batch.trigger, batch.trigger)


def batch_summary(batch, runs=None) -> dict:
    """One line of history: how it was started, how it went, how long it
    took, on which release of the site."""
    runs = list(batch.runs.all()) if runs is None else runs
    counts = {}
    for r in runs:
        counts[r.status] = counts.get(r.status, 0) + 1
    total, passed = len(runs), counts.get(Run.PASSED, 0)
    active = any(r.status in Run.ACTIVE_STATUSES for r in runs)
    starts = [r.started_at for r in runs if r.started_at]
    ends = [r.finished_at for r in runs if r.finished_at]
    return {
        "batch": batch,
        "source": batch_source(batch, runs),
        "total": total,
        "passed": passed,
        "others": sorted(((s, n) for s, n in counts.items() if s != Run.PASSED),
                         key=lambda sn: -sn[1]),
        "active": active,
        "all_passed": bool(total) and passed == total,
        "rate": None if active or not total else round(100.0 * passed / total),
        "wall": (max(ends) - min(starts)).total_seconds() if starts and ends and not active else None,
        "test_time": sum(r.duration_seconds or 0 for r in runs),
        "release": ", ".join(sorted({r.target_version for r in runs if r.target_version})),
        "started": min(starts) if starts else None,
        "finished": max(ends) if ends and not active else None,
    }


def usual_duration(test, before_pk, exclude_batch=None) -> "float | None":
    """What this test usually takes: the MEDIAN of its earlier ordinary runs
    (load-test iterations excluded) -- a median, so one 60 s outlier does
    not move it. None with too little history to say."""
    qs = Run.regular().filter(test=test, status__in=Run.REPRESENTATIVE_STATUSES,
                              duration_seconds__isnull=False, pk__lt=before_pk)
    if exclude_batch is not None:
        qs = qs.exclude(batch=exclude_batch)
    values = list(qs.order_by("-pk").values_list("duration_seconds", flat=True)[:USUAL_SAMPLE])
    return percentile(values, 50) if len(values) >= USUAL_MIN else None


def versus_usual(seconds, usual) -> "tuple[str, str]":
    """("34% slower", "slower") / ("20% faster", "faster") / ("as usual", "")."""
    if seconds is None or not usual:
        return "", ""
    ratio = seconds / usual
    if ratio >= SLOWER:
        return f"{round((ratio - 1) * 100)}% slower", "slower"
    if ratio <= FASTER:
        return f"{round((1 - ratio) * 100)}% faster", "faster"
    return "as usual", ""


# --------------------------------------------------------------------------
# what is running: when each run -- and each group run -- should be done
# --------------------------------------------------------------------------

def queue_forecast(active_runs, workers, estimates=None) -> dict:
    """Seconds from now until each active run should FINISH, simulating the
    runner itself: `workers` slots; a running run holds its slot until its
    estimate runs out; queued runs take the next free slot in queue order
    (`active_runs` must be in that order). {run pk: seconds, or None where
    a test has no history yet -- and for whatever queues behind a slot whose
    end is unknown}. Summing the estimates and dividing by the workers (the
    old batch-page figure) is wrong whenever durations differ: three 10 s
    runs on two workers end at 20 s, not 15."""
    estimates = {} if estimates is None else estimates

    def estimate(run):
        if run.test_id not in estimates:
            estimates[run.test_id] = estimate_seconds(run.test)
        return estimates[run.test_id]

    ends, slots = {}, []
    for run in active_runs:
        if run.status == Run.RUNNING:
            est = estimate(run)
            left = None if est is None else max(0.0, est - (run.elapsed_seconds or 0.0))
            ends[run.pk] = left
            slots.append(left)
    slots += [0.0] * max(0, workers - len(slots))
    for run in active_runs:
        if run.status != Run.QUEUED:
            continue
        free = [(t, i) for i, t in enumerate(slots) if t is not None]
        est = estimate(run)
        if not free:
            ends[run.pk] = None          # every slot is busy for an unknown time
            continue
        start, slot = min(free)
        ends[run.pk] = None if est is None else start + est
        slots[slot] = ends[run.pk]       # an unknown duration makes the slot unknown too
    return ends


def active_batches(active_runs, ends) -> list:
    """The group runs (and multi-test batches) in progress, in queue order:
    what is done, what is left, and when the last of it should finish."""
    now = timezone.now()
    by_batch = {}
    for run in active_runs:
        if run.batch_id is not None:
            by_batch.setdefault(run.batch_id, []).append(run)
    out = []
    for active in by_batch.values():
        batch = active[0].batch
        runs = list(batch.runs.all())
        if len(runs) < 2 and not batch.group_id:
            continue                     # a single run is just a run
        counts = {}
        for r in runs:
            counts[r.status] = counts.get(r.status, 0) + 1
        left = [ends.get(r.pk) for r in active]
        starts = [r.started_at for r in runs if r.started_at]
        sched = batch.schedule
        out.append({
            "id": batch.pk,
            "label": batch.label,
            "group": batch.group.name if batch.group_id else "",
            "source": batch_source(batch, runs),
            "schedule": (f"{sched.days_display} {sched.time_of_day:%H:%M}" if sched else ""),
            "total": len(runs),
            "done": sum(n for s, n in counts.items() if s not in Run.ACTIVE_STATUSES),
            "passed": counts.get(Run.PASSED, 0),
            "failed": sum(counts.get(s, 0) for s in (Run.FAILED, Run.ERROR, Run.TIMEOUT)),
            "running": counts.get(Run.RUNNING, 0),
            "queued": counts.get(Run.QUEUED, 0),
            "elapsed": round((now - min(starts)).total_seconds(), 1) if starts else 0.0,
            "eta": None if any(x is None for x in left) else round(max(left), 1),
        })
    return out


def sparkline(test, points=20) -> dict:
    """Tiny inline trend for list rows: recent outcomes + durations."""
    runs = list(
        Run.regular().filter(test=test, status__in=Run.FINISHED_STATUSES)
        .order_by("-queued_at")[:points]
    )
    runs.reverse()
    durations = [r.duration_seconds or 0 for r in runs]
    top = max(durations) if durations else 1
    return {
        "points": [
            {"x": i, "h": (d / top * 100) if top else 0,
             "status": r.status}
            for i, (r, d) in enumerate(zip(runs, durations))
        ],
        "n": len(runs),
    }

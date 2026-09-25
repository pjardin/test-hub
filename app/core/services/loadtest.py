"""Load and performance testing, built on the tests you already have.

The idea is deliberately small: take ONE existing test and run it over and
over, several copies at a time, for a fixed stretch of wall-clock time. No
second scripting language, no separate engine, no recording a new scenario
-- if a test can drive the application once, it can drive it two hundred
times while you watch what happens.

WHY NOT JMETER-STYLE HTTP LOAD
Two different questions, and this answers the second one:

  * "can the server take 5000 requests a second?" -- that is a protocol-level
    question and JMeter/k6/Locust answer it far better than a browser can.
  * "does the APPLICATION still work, and still feel fast, when 20 people are
    using it at once?" -- that needs a real browser: real JavaScript, real
    rendering, real waits for elements to appear. That is what this does.

So the numbers here are end-user response times, not request counts. A
handful of real browsers finding that the dashboard takes 9s instead of 1.2s
tells you something no request-per-second figure will.

WHAT IT COSTS
Every virtual user is a real Chromium: roughly 250-400 MB of RAM and a chunk
of a CPU core. Twenty of them is a serious machine. `capacity_hint()` below
turns the box's actual memory into an honest ceiling, and the planner refuses
to pretend otherwise -- a load test that swaps is measuring the swap.

THE PLAN COMES FROM A REAL RUN
You cannot say "run it for 10 minutes" without knowing how long one pass
takes. `baseline_for()` looks at the test's own recent history (or runs it
once), and from that a duration and a concurrency give you a projected
iteration count before you commit to anything.
"""
import json
import logging
import math
import os
import threading
import time

from django.utils import timezone

from core import appconfig
from core.models import LoadRun, Run, Test

log = logging.getLogger("testhub.loadtest")

# One virtual user is one Chromium. Measured on the bundled builds: a browser
# driving a simple page settles around 250-350 MB RSS, so 400 MB per user is
# the number to plan with -- being wrong in this direction costs a slower
# test, being wrong the other way costs a swapping machine and garbage data.
MB_PER_VIRTUAL_USER = 400
RESERVED_SYSTEM_MB = 1024          # leave the OS (and the hub itself) room
HARD_MAX_CONCURRENCY = 64          # a backstop, not a recommendation

_active = {}                       # load_run pk -> executor
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def _total_memory_mb():
    """Physical RAM in MB, or None if the platform will not say."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (pages * page_size) / 1e6
    except (ValueError, OSError, AttributeError):
        return None


def capacity_hint() -> dict:
    """How many virtual users this machine can honestly carry.

    Not advice to max it out: it is the point past which the numbers stop
    describing the application and start describing the machine.
    """
    total_mb = _total_memory_mb()
    cpus = os.cpu_count() or 1
    if total_mb is None:
        return {"suggested": 3, "ceiling": 8, "total_mb": None, "cpus": cpus,
                "why": "could not read this machine's memory; assuming a small box"}
    usable = max(0, total_mb - RESERVED_SYSTEM_MB)
    by_memory = int(usable // MB_PER_VIRTUAL_USER)
    # Browsers are not purely CPU-bound (they spend a lot of time waiting on
    # the target), so allow a few per core -- but not unlimited.
    by_cpu = cpus * 3
    ceiling = max(1, min(by_memory, by_cpu, HARD_MAX_CONCURRENCY))
    return {
        "suggested": max(1, min(ceiling, 5)),
        "ceiling": ceiling,
        "total_mb": round(total_mb),
        "cpus": cpus,
        "why": (f"{round(total_mb/1024, 1)} GB RAM and {cpus} CPU(s): "
                f"~{MB_PER_VIRTUAL_USER} MB per browser leaves room for "
                f"about {ceiling} at once"),
    }


def baseline_for(test: Test, sample=10) -> dict:
    """How long ONE pass takes when nothing else is going on.

    Load iterations are excluded, and that exclusion is the whole point. They
    are by definition measured while the machine is busy, and they outnumber
    solo runs within minutes -- one 10-minute load test adds ~1300 of them.
    Counting them makes the "baseline" the LOADED time, so the next load test
    compares loaded against loaded and proudly reports 1.0x slowdown.
    (Measured: after two load runs, all 10 of the most recent runs were load
    iterations and the slowdown factor read 0.99x on a run that was genuinely
    slower.)

    Returns {seconds, source, samples}. `seconds` is None when the test has
    never completed on its own -- the caller should run it once first.
    """
    durations = list(
        Run.objects.filter(test=test, status__in=Run.REPRESENTATIVE_STATUSES,
                           duration_seconds__isnull=False, load_run__isnull=True)
        .order_by("-queued_at").values_list("duration_seconds", flat=True)[:sample])
    if not durations:
        return {"seconds": None, "source": "none", "samples": 0}
    ordered = sorted(durations)
    median = ordered[len(ordered) // 2]
    return {"seconds": round(median, 3), "source": "history",
            "samples": len(durations),
            "fastest": round(ordered[0], 3), "slowest": round(ordered[-1], 3)}


def project(baseline_seconds, duration_seconds, concurrency, think_time=0.0,
            max_iterations=0) -> dict:
    """Turn "10 minutes at 5 users" into "about this many iterations".

    Honest about being an estimate: it assumes the app keeps up. The whole
    point of running it is to find out whether it does, and a real result
    below this projection IS the finding.
    """
    if not baseline_seconds or baseline_seconds <= 0:
        return {"iterations": 0, "per_user": 0, "rate_per_minute": 0,
                "note": "no baseline yet -- run the test once first"}
    cycle = baseline_seconds + max(0.0, think_time)
    per_user = max(1, int(duration_seconds // cycle))
    total = per_user * max(1, concurrency)
    if max_iterations:
        total = min(total, max_iterations)
    return {
        "iterations": total,
        "per_user": per_user,
        "cycle_seconds": round(cycle, 2),
        "rate_per_minute": round(total / (duration_seconds / 60.0), 1)
        if duration_seconds else 0,
        "note": (f"about {total} passes if the application keeps up at "
                 f"{baseline_seconds:.1f}s each; fewer means it slowed down, "
                 f"which is the result you are looking for"),
    }


def plan_preview(test: Test, duration_seconds, concurrency, think_time=0.0,
                 max_iterations=0) -> dict:
    """Everything the UI needs to show before anyone commits to a run."""
    base = baseline_for(test)
    projection = project(base["seconds"], duration_seconds, concurrency,
                         think_time, max_iterations)
    capacity = capacity_hint()
    warnings = []
    if concurrency > capacity["ceiling"]:
        warnings.append(
            f"{concurrency} browsers is beyond what this machine can hold "
            f"(~{capacity['ceiling']}). The numbers would describe the machine, "
            f"not the application.")
    if base["seconds"] and duration_seconds < base["seconds"] * 2:
        warnings.append(
            f"the window ({duration_seconds}s) is barely longer than one pass "
            f"({base['seconds']:.1f}s) -- too short to show a trend")
    if not base["seconds"]:
        warnings.append("this test has no completed run yet, so there is "
                        "nothing to base a projection on -- run it once first")
    return {"baseline": base, "projection": projection, "capacity": capacity,
            "warnings": warnings}


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

class LoadExecutor:
    """Keeps `concurrency` copies of one test in flight until time runs out.

    Its own threads on purpose: it must not consume the normal runner's
    worker pool, or starting a load test would stall everybody else's runs.
    """

    def __init__(self, load_run: LoadRun):
        self.load_run = load_run
        self.stop_event = threading.Event()
        self.threads = []
        self.counter = threading.Lock()
        self.started_iterations = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        lr = self.load_run
        lr.status = LoadRun.RUNNING
        lr.started_at = timezone.now()
        lr.save(update_fields=["status", "started_at"])
        with _lock:
            _active[lr.pk] = self
        deadline = time.monotonic() + lr.duration_seconds
        for index in range(max(1, lr.concurrency)):
            thread = threading.Thread(
                target=self._virtual_user, args=(index, deadline),
                name=f"load-{lr.pk}-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)
        threading.Thread(target=self._await_finish, daemon=True,
                         name=f"load-{lr.pk}-watch").start()
        log.info("load run %s started: %s users for %ss on %s",
                 lr.pk, lr.concurrency, lr.duration_seconds, lr.test.test_id)

    def stop(self):
        self.stop_event.set()

    # -- one virtual user ---------------------------------------------------
    def _virtual_user(self, index, deadline):
        try:
            self._drive(index, deadline)
        finally:
            # A thread that used the ORM owns a database connection until it
            # says otherwise. These threads are created per load run and then
            # die, so without this every run leaks one SQLite handle per
            # virtual user, for the life of the server process. (Measured: 8
            # non-closing threads left 18 handles behind.)
            from django.db import connections
            connections.close_all()

    def _drive(self, index, deadline):
        from django.db import close_old_connections
        from core.services.runner import execute_run

        lr = self.load_run
        # Ramp: stagger the starts so the graph shows WHERE it began to hurt,
        # instead of every user hitting at t=0 and blurring the cause.
        if lr.ramp_seconds and lr.concurrency > 1:
            delay = (lr.ramp_seconds / lr.concurrency) * index
            if self.stop_event.wait(timeout=delay):
                return

        while not self.stop_event.is_set():
            if time.monotonic() >= deadline:
                return
            close_old_connections()
            if self._kill_requested():
                self.stop_event.set()
                return
            with self.counter:
                if lr.max_iterations and self.started_iterations >= lr.max_iterations:
                    return
                self.started_iterations += 1
                iteration = self.started_iterations

            run = Run.objects.create(
                test=lr.test, load_run=lr, iteration=iteration,
                trigger="load", target_url=lr.target_url,
                target_version=lr.target_version,
                browser=lr.browser, headed=False,
                minimal_artifacts=not lr.keep_artifacts,
            )
            try:
                execute_run(run.pk)
            except Exception:
                log.exception("load run %s: iteration %s crashed", lr.pk, iteration)

            if lr.think_time_seconds:
                if self.stop_event.wait(timeout=lr.think_time_seconds):
                    return

    def _kill_requested(self):
        return LoadRun.objects.filter(pk=self.load_run.pk,
                                      kill_requested=True).exists()

    # -- finish -------------------------------------------------------------
    def _await_finish(self):
        try:
            self._finish()
        finally:
            from django.db import connections
            connections.close_all()

    def _finish(self):
        from django.db import close_old_connections
        for thread in self.threads:
            thread.join()
        close_old_connections()
        lr = LoadRun.objects.filter(pk=self.load_run.pk).first()
        if lr is None:
            return
        lr.finished_at = timezone.now()
        lr.status = LoadRun.KILLED if lr.kill_requested else LoadRun.FINISHED
        lr.summary_json = json.dumps(summarize(lr))
        lr.save(update_fields=["finished_at", "status", "summary_json"])
        with _lock:
            _active.pop(lr.pk, None)
        log.info("load run %s %s: %s iteration(s)", lr.pk, lr.status,
                 lr.iterations.count())


def start_load_run(test: Test, duration_seconds=600, concurrency=3,
                   ramp_seconds=0, think_time=0.0, max_iterations=0,
                   browser="", keep_artifacts=False, plan=None,
                   label="") -> LoadRun:
    """Create and start a load run. Raises ValueError on a refusal."""
    cfg = appconfig.get_config()
    concurrency = max(1, int(concurrency))
    capacity = capacity_hint()
    if concurrency > HARD_MAX_CONCURRENCY:
        raise ValueError(
            f"{concurrency} simultaneous browsers is beyond what this tool will "
            f"start ({HARD_MAX_CONCURRENCY}). Each one is a real Chromium.")

    # Count what is ALREADY loading this machine. Checking the request in
    # isolation let three 5-user runs start against a 5-user ceiling: 15
    # browsers, a swapping box, and three sets of meaningless numbers.
    already = sum(LoadRun.objects.filter(status__in=LoadRun.ACTIVE_STATUSES)
                  .values_list("concurrency", flat=True))
    if already and already + concurrency > capacity["ceiling"]:
        raise ValueError(
            f"a load test is already running {already} browser(s); {concurrency} "
            f"more would exceed what this machine can hold "
            f"(~{capacity['ceiling']}). Wait for it to finish, or stop it "
            f"first -- overlapping load tests measure each other.")
    # A load test that fills the disk corrupts SQLite and loses the results
    # it was run to collect. Same guard the ordinary runner uses, earlier.
    try:
        import shutil
        free_mb = shutil.disk_usage(str(cfg.data_dir)).free / 1e6
    except OSError:
        free_mb = None
    if free_mb is not None and free_mb < 500:
        raise ValueError(
            f"only {free_mb:.0f} MB free where results are stored -- a load "
            f"test writes many runs. Prune artifacts first (Settings).")

    base = baseline_for(test)
    projection = project(base["seconds"], duration_seconds, concurrency,
                         think_time, max_iterations)
    lr = LoadRun.objects.create(
        plan=plan, test=test,
        label=label or f"load: {test.test_id}",
        duration_seconds=int(duration_seconds), concurrency=concurrency,
        ramp_seconds=int(ramp_seconds), think_time_seconds=float(think_time),
        max_iterations=int(max_iterations),
        browser=browser or cfg.browser, keep_artifacts=bool(keep_artifacts),
        baseline_seconds=base["seconds"], baseline_source=base["source"],
        projected_iterations=projection["iterations"],
        target_url=cfg.target_url, target_version=cfg.target_version,
    )
    LoadExecutor(lr).start()
    return lr


def kill_load_run(load_run_id: int) -> bool:
    """Stop applying load. In-flight iterations are left to finish on their
    own -- killing them mid-flight would poison the very timings being
    collected, and they are seconds from ending anyway."""
    updated = LoadRun.objects.filter(pk=load_run_id,
                                     status__in=LoadRun.ACTIVE_STATUSES) \
                             .update(kill_requested=True)
    if not updated:
        return False
    with _lock:
        executor = _active.get(load_run_id)
    if executor:
        executor.stop()
    return True


def recover_orphans():
    """Load runs left active by a previous process are dead. Called at
    `serve` startup, where 'nothing is executing' actually holds."""
    stale = LoadRun.objects.filter(status__in=LoadRun.ACTIVE_STATUSES)
    n = stale.update(status=LoadRun.ERROR, finished_at=timezone.now(),
                     error_message="the server restarted while this load test "
                                   "was running")
    if n:
        log.warning("marked %d orphaned load run(s) as error", n)
    return n


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

def _percentile(ordered, fraction):
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(load_run: LoadRun) -> dict:
    """The headline numbers: how many, how fast, how many broke.

    Percentiles rather than an average, because an average hides the tail --
    and the tail is what a user actually complains about.
    """
    runs = list(load_run.iterations.all()
                .values("status", "duration_seconds", "started_at", "error_message"))
    done = [r for r in runs if r["duration_seconds"] is not None]
    durations = sorted(r["duration_seconds"] for r in done)
    passed = sum(1 for r in runs if r["status"] == Run.PASSED)
    failed = sum(1 for r in runs
                 if r["status"] in (Run.FAILED, Run.ERROR, Run.TIMEOUT))
    elapsed = load_run.elapsed_seconds or 0

    summary = {
        "iterations": len(runs),
        "completed": len(done),
        "passed": passed,
        "failed": failed,
        "error_rate": round(100.0 * failed / len(runs), 1) if runs else 0.0,
        "projected": load_run.projected_iterations,
        "elapsed_seconds": round(elapsed, 1),
        "throughput_per_minute": round(len(done) / (elapsed / 60.0), 1)
        if elapsed > 1 else 0.0,
        "baseline_seconds": load_run.baseline_seconds,
    }
    if durations:
        summary.update({
            "min": round(durations[0], 3),
            "max": round(durations[-1], 3),
            "avg": round(sum(durations) / len(durations), 3),
            "p50": round(_percentile(durations, 0.50), 3),
            "p90": round(_percentile(durations, 0.90), 3),
            "p95": round(_percentile(durations, 0.95), 3),
            "p99": round(_percentile(durations, 0.99), 3),
        })
        # The number people actually want: is it slower under load than it
        # was on its own, and by how much?
        if load_run.baseline_seconds:
            summary["slowdown_x"] = round(
                summary["p50"] / load_run.baseline_seconds, 2)
    return summary


def timeline(load_run: LoadRun, buckets=60) -> list:
    """Response time and throughput over the life of the run.

    This is the chart that earns the feature: a flat line means the
    application shrugged it off, a line that climbs means you found the
    limit, and where it starts climbing is the answer.
    """
    runs = list(load_run.iterations.exclude(started_at=None)
                .exclude(duration_seconds=None)
                .values("started_at", "duration_seconds", "status")
                .order_by("started_at"))
    if not runs:
        return []
    start = load_run.started_at or runs[0]["started_at"]
    span = max(1.0, (load_run.elapsed_seconds or 1.0))
    width = span / max(1, buckets)
    grouped = {}
    for run in runs:
        offset = (run["started_at"] - start).total_seconds()
        index = min(buckets - 1, max(0, int(offset // width)))
        bucket = grouped.setdefault(index, {"durations": [], "failed": 0})
        bucket["durations"].append(run["duration_seconds"])
        if run["status"] != Run.PASSED:
            bucket["failed"] += 1
    out = []
    for index in sorted(grouped):
        bucket = grouped[index]
        ordered = sorted(bucket["durations"])
        out.append({
            "t": round(index * width, 1),
            "n": len(ordered),
            "avg": round(sum(ordered) / len(ordered), 3),
            "p95": round(_percentile(ordered, 0.95), 3),
            "failed": bucket["failed"],
        })
    return out


def step_breakdown(load_run: LoadRun, limit=12) -> list:
    """Per-step timings aggregated across every iteration.

    The differentiator over a protocol-level load tool: the hub already
    records how long each click and each wait took, so it can say WHICH step
    degrades. "The login POST is fine, it is the dashboard render that falls
    over at 8 users" is an actionable sentence; "p95 went up" is not.
    """
    steps = {}
    for timings_json in load_run.iterations.exclude(timings_json="[]") \
            .values_list("timings_json", flat=True):
        try:
            entries = json.loads(timings_json or "[]")
        except ValueError:
            continue
        for entry in entries:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            steps.setdefault(name, []).append(entry.get("ms", 0) / 1000.0)
    out = []
    for name, seconds in steps.items():
        ordered = sorted(seconds)
        out.append({
            "name": name,
            "count": len(ordered),
            "avg": round(sum(ordered) / len(ordered), 3),
            "p95": round(_percentile(ordered, 0.95), 3),
            "max": round(ordered[-1], 3),
        })
    out.sort(key=lambda s: s["p95"], reverse=True)
    return out[:limit]


def live_status(load_run: LoadRun) -> dict:
    """What the watching page polls while a load test is in flight."""
    running = load_run.iterations.filter(status=Run.RUNNING).count()
    summary = summarize(load_run)
    remaining = None
    if load_run.is_active and load_run.started_at:
        remaining = max(0.0, load_run.duration_seconds -
                        (load_run.elapsed_seconds or 0))
    return {
        "ok": True,
        "status": load_run.status,
        "active": load_run.is_active,
        "in_flight": running,
        "remaining_seconds": round(remaining, 1) if remaining is not None else None,
        "summary": summary,
    }

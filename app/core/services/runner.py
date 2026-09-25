"""Run execution: queue, worker pool, subprocess control, kill.

Design:

  * Each run is a SUBPROCESS (core/harness/run_test.py) started in its own
    session, so killing a run kills the whole process group -- Playwright,
    its driver, and the browser it launched -- not just the wrapper.
  * Worker threads (config runner.max_parallel) pull queued run ids. That is
    what "run multiple at the same time" means mechanically.
  * Kill is DB-first: the UI sets Run.kill_requested, and whichever process
    is executing the run notices within a poll tick and terminates the
    process group. This works from the web process, from `manage.py runtest`,
    and across restarts, with the in-memory registry as a fast path.
  * The runner lives ONLY in `manage.py serve` (and, synchronously, in
    `manage.py runtest`). Plain `runserver` gets a read-only UI.
"""
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from django.db import close_old_connections
from django.utils import timezone

from core import appconfig
from core.models import Batch, Run, Test

log = logging.getLogger("testhub.runner")

HARNESS = Path(__file__).resolve().parent.parent / "harness" / "run_test.py"
# TypeScript tests run through an adapter with the SAME argv contract: it
# spawns the TS engine's @playwright/test (/opt/pw-ts) and writes the same
# result.json, so everything in this file stays language-blind.
TS_HARNESS = Path(__file__).resolve().parent.parent / "harness" / "run_ts_test.py"

# Seconds added on top of the test timeout before the manager hard-kills:
# the harness enforces the timeout itself first and exits cleanly with
# artifacts; this grace period only catches a wedged harness.
KILL_GRACE = 20

_runner = None
_runner_lock = threading.Lock()


def get():
    return _runner


def install(runner):
    global _runner
    with _runner_lock:
        _runner = runner
    return runner


# ---------------------------------------------------------------------------
# single-run execution (used by worker threads AND `manage.py runtest`)
# ---------------------------------------------------------------------------

def _terminate_group(proc, reason, grace=8):
    """Stop a run, preserving whatever artifacts we can.

    Order matters. The harness installs a SIGTERM handler that raises inside
    the test so its `finally` can close the context (which is what finalises
    video.webm) and write result.json. Flattening the whole group at once
    also SIGTERMs Chromium, so the browser can die mid-teardown and the video
    is lost. So: signal the harness alone, give it a short grace, and only
    escalate to the group for whatever is still standing.
    """
    log.info("terminating run pid=%s (%s)", proc.pid, reason)
    try:
        os.kill(proc.pid, signal.SIGTERM)      # harness only
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=grace)
        # It exited cleanly, but Chromium helpers may outlive it.
        _kill_group_quietly(proc, signal.SIGKILL)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("process group %s refused to die", proc.pid)


def _kill_group_quietly(proc, sig):
    """Signal the process group, ignoring 'already gone'. Used to sweep up
    Chromium/ffmpeg children that outlive the harness."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _read_result(artifacts_dir: Path) -> dict:
    import json
    path = artifacts_dir / "result.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def execute_run(run_id: int):
    """Run one queued Run to completion. Safe to call from any thread or
    process; assumes exclusive ownership of that run id."""
    cfg = appconfig.get_config()
    close_old_connections()
    try:
        run = Run.objects.select_related("test").get(pk=run_id)
    except Run.DoesNotExist:
        return

    if run.status != Run.QUEUED:
        return  # killed while queued, or picked up elsewhere
    if run.kill_requested:
        _finalize(run, Run.KILLED, error="killed before start")
        return

    test = run.test
    test_path = cfg.tests_dir / test.file_path
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-r{run.pk}"
    artifacts_rel = f"{test.test_id}/{stamp}"
    artifacts_dir = cfg.results_dir / artifacts_rel
    run.artifacts_rel = artifacts_rel

    if not test_path.exists():
        _finalize(run, Run.ERROR, error=f"test file missing: {test.file_path}")
        return

    # Disk guard: a full disk corrupts SQLite and produces baffling
    # failures. Refuse clearly instead, while the hub is still healthy.
    try:
        import shutil as _shutil
        free_mb = _shutil.disk_usage(str(cfg.data_dir)).free / 1e6
    except OSError:
        free_mb = None
    if free_mb is not None and free_mb < 200:
        _finalize(run, Run.ERROR,
                  error=f"refusing to run: only {free_mb:.0f} MB free on the "
                        f"results disk. Prune old artifacts (Settings page) or "
                        f"free space, then run again.")
        return

    # Only now, once we know the run will actually start: creating this first
    # littered an empty folder per refused run, and on a full disk the mkdir
    # is the thing that raises.
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    timeout = test.timeout_seconds or cfg.default_timeout
    python = cfg.harness_python or sys.executable
    is_ts = getattr(test, "test_type", "") == Test.TYPE_TS
    cmd = [
        python, str(TS_HARNESS if is_ts else HARNESS),
        "--test-file", str(test_path),
        "--artifacts-dir", str(artifacts_dir),
        "--base-url", run.target_url or cfg.target_url,
        "--timeout", str(timeout),
        "--browser", run.browser or cfg.browser,
        "--test-id", test.test_id,
    ]
    if run.headed:
        cmd.append("--headed")
    # Per-test override beats the global toggle: a long-wait test can opt
    # out of an 80 MB video of a countdown without disabling video for
    # everything else.
    want_video = cfg.video
    if test.video_mode == Test.VIDEO_ON:
        want_video = True
    elif test.video_mode == Test.VIDEO_OFF:
        want_video = False

    # A load iteration records nothing: hundreds of identical videos is
    # gigabytes of disk to repeat what the step timings already say, and the
    # recording overhead would distort the very numbers being measured.
    if run.minimal_artifacts:
        want_video = False
    if want_video:
        cmd.append("--video")
    if cfg.trace and not run.minimal_artifacts:
        cmd.append("--trace")
    if cfg.live_screenshots and not run.minimal_artifacts:
        cmd.append("--live-screenshots")
    if cfg.activity_frames and not run.minimal_artifacts:
        cmd.append("--activity-frames")
    # Security observation stays on even for load iterations: it is a few
    # header lookups, and a misconfiguration is worth catching whichever run
    # happens to notice it. It lives inside the PYTHON harness's page, so
    # TypeScript runs skip it (stated in the UI) rather than pretending.
    if cfg.security_checks and not is_ts:
        cmd.append("--security")
    # Credentials for tests that log in. The path is passed, not the values:
    # environment variables are visible to other users via /proc on a shared
    # machine, and the file itself is 0600.
    from core import secretstore
    secrets_path = secretstore.path_for(cfg.data_dir)
    if secrets_path.exists():
        secretstore.harden(secrets_path)
        cmd.extend(["--secrets-file", str(secrets_path)])

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    # Hold the machine awake for as long as this run takes. Reference
    # counted, so ten parallel runs share one lock and an idle hub still
    # lets a laptop sleep.
    from core.services import keepawake
    keepawake.acquire(f"running {test.test_id}")
    try:
        _execute_run_body(run, test, cfg, cmd, env, artifacts_dir, timeout)
    finally:
        # try/finally, NOT a manual acquire/release pair: there are early
        # returns between the two (a harness that fails to start, a process
        # that will not die), and each one would have leaked the lock and
        # left the machine awake for good.
        keepawake.release()


def _execute_run_body(run, test, cfg, cmd, env, artifacts_dir, timeout):
    log_path = artifacts_dir / "run.log"
    with open(log_path, "wb") as log_fh:
        try:
            proc = subprocess.Popen(
                cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                cwd=str(cfg.tests_dir), env=env, start_new_session=True,
            )
        except OSError as exc:
            _finalize(run, Run.ERROR, error=f"could not start harness: {exc}")
            return

        run.status = Run.RUNNING
        run.started_at = timezone.now()
        run.pid = proc.pid
        run.save(update_fields=["status", "started_at", "pid", "artifacts_rel"])
        log.info("run %s (%s) started, pid=%s", run.pk, test.test_id, proc.pid)

        killed_reason = None
        last_flag_check = 0.0
        started_monotonic = time.monotonic()
        # Trace/video finalisation happens AFTER the harness's own timeout, so
        # the hard-kill grace has to be generous enough not to truncate it.
        grace = KILL_GRACE + (40 if (cfg.trace or cfg.video) else 0)
        while proc.poll() is None:
            time.sleep(0.5)
            now = time.monotonic()
            if now - last_flag_check >= 2.0:
                last_flag_check = now
                close_old_connections()
                if Run.objects.filter(pk=run.pk, kill_requested=True).exists():
                    killed_reason = "kill"
                    # Same generous window as the timeout path: finalising a
                    # video takes far longer than the 8s default, and cutting
                    # it short is what loses the artifacts a tester pressed
                    # Kill to go and look at.
                    _terminate_group(proc, "kill requested", grace=grace)
                    break
            # Monotonic, not wall-clock: an air-gapped box has no NTP and
            # someone WILL run `date -s`. A backward step used to make this
            # negative (hard-kill never fires); a forward step killed healthy
            # runs instantly.
            if now - started_monotonic > timeout + grace:
                killed_reason = "timeout"
                _terminate_group(proc, f"timeout ({timeout}s + {grace}s grace)",
                                 grace=grace)
                break
        try:
            # Bounded: _terminate_group can give up on a process wedged in
            # uninterruptible sleep (a stalled disk). An unbounded wait here
            # would park this worker thread forever and leave the run stuck
            # in "running" with no way to finish or kill it.
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log.error("run %s: process %s will not die; giving up on it",
                      run.pk, proc.pid)
            close_old_connections()
            _finalize(run, Run.ERROR,
                      error="the test process could not be stopped (the machine "
                            "may have a stalled disk); this worker was released",
                      exit_code=None)
            return

    close_old_connections()
    run.refresh_from_db()
    result = _read_result(artifacts_dir)
    rc = proc.returncode

    # The supervision loop only re-reads the kill flag every 2s, but
    # kill_run() signals the process immediately -- so a harness that exits
    # quickly (e.g. video/trace disabled) is reaped before the loop notices,
    # and the run would be filed as ERROR. Trust the refreshed row and the
    # harness's own verdict too.
    if killed_reason is None and (run.kill_requested
                                  or result.get("status") == "killed"):
        killed_reason = "kill"

    if killed_reason == "kill":
        status = Run.KILLED
    elif killed_reason == "timeout" or result.get("status") == "timeout":
        status = Run.TIMEOUT
    elif result.get("status") in (Run.PASSED, Run.FAILED, Run.ERROR):
        status = result["status"]
    elif rc == 0:
        status = Run.PASSED
    elif rc == 1:
        status = Run.FAILED
    else:
        status = Run.ERROR

    error = result.get("error") or ""
    if status == Run.ERROR and not error:
        error = _tail(log_path)
    if status == Run.KILLED and not error:
        error = "stopped by request (Kill)"
    if status == Run.TIMEOUT and not error:
        error = f"the test ran longer than its {timeout}s time limit"
    timings = result.get("timings")
    if isinstance(timings, list):
        import json as _json
        run.timings_json = _json.dumps(timings[:300])

    # security.json is its own artifact (written after the verdict, so it can
    # never delay it). Fold it onto the row so pages do not read files.
    import json as _json
    sec_path = artifacts_dir / "security.json"
    if sec_path.exists():
        try:
            report = _json.loads(sec_path.read_text(encoding="utf-8"))
            report["findings"] = report.get("findings", [])[:100]
            run.security_json = _json.dumps(report)
        except (OSError, ValueError):
            pass
    if run.security_json and run.security_json != "{}":
        run.save(update_fields=["security_json"])
    _finalize(run, status, error=error, exit_code=rc,
              duration=result.get("duration_seconds"))

    # Optional: push artifacts to object storage (no-op unless configured).
    try:
        from core.services import storage
        if storage.enabled():
            storage.upload_run(run)
    except Exception:
        log.exception("artifact offload failed for run %s (kept locally)", run.pk)


def _tail(path: Path, limit=2000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def _finalize(run, status, error="", exit_code=None, duration=None):
    run.status = status
    run.finished_at = timezone.now()
    if duration is None and run.started_at:
        duration = (run.finished_at - run.started_at).total_seconds()
    run.duration_seconds = duration
    run.error_message = (error or "")[:10000]
    run.exit_code = exit_code
    run.pid = None
    run.save()
    log.info("run %s (%s) finished: %s (%.1fs)",
             run.pk, run.test.test_id, status, duration or 0)


# ---------------------------------------------------------------------------
# the queue + worker pool
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, cfg=None):
        self.cfg = cfg or appconfig.get_config()
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.threads = []

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        self.recover_orphans()
        for i in range(self.cfg.max_parallel):
            t = threading.Thread(target=self._worker, name=f"runner-{i}", daemon=True)
            t.start()
            self.threads.append(t)
        adopt = threading.Thread(target=self._adopt_cli_runs,
                                 name="runner-cli-adopt", daemon=True)
        adopt.start()
        self.threads.append(adopt)
        log.info("runner started: %d parallel workers", self.cfg.max_parallel)

    def _adopt_cli_runs(self):
        """Terminal commands (`testhub run`) create QUEUED rows from ANOTHER
        process; only this serve process executes runs, so it adopts them.
        The atomic trigger flip IS the claim -- a row is queued exactly once
        even with several pollers, and in-process enqueues (other triggers)
        can never collide with it."""
        while not self.stop_event.is_set():
            try:
                close_old_connections()
                pks = list(Run.objects.filter(status=Run.QUEUED, trigger="cli")
                           .values_list("pk", flat=True))
                for pk in pks:
                    claimed = Run.objects.filter(
                        pk=pk, status=Run.QUEUED, trigger="cli",
                    ).update(trigger="cli-adopted")
                    if claimed:
                        self.queue.put(pk)
            except Exception:
                log.exception("cli-run adoption failed (will retry)")
            self.stop_event.wait(2.0)

    def stop(self):
        self.stop_event.set()

    def shutdown(self, grace=10):
        """Stop accepting work and terminate anything still executing.

        Called from `serve`'s finally block. Active runs are marked ABORTED
        here rather than left 'running' forever: the next start would recover
        them anyway, but a clean stop should not need recovery."""
        self.stop_event.set()
        live = list(Run.objects.filter(status__in=Run.ACTIVE_STATUSES)
                    .exclude(pid=None).values_list("pk", "pid"))
        for pk, pid in live:
            # Harness first (same reason as kill_run): it saves result.json
            # and finalises the video from its SIGTERM handler. The group is
            # swept below once the grace period is up.
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                continue
        deadline = time.monotonic() + grace
        for t in self.threads:
            t.join(timeout=max(0.1, deadline - time.monotonic()))
        for pk, pid in live:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        n = (Run.objects.filter(status__in=Run.ACTIVE_STATUSES)
             .update(status=Run.ABORTED, finished_at=timezone.now(),
                     error_message="the server was shut down while this run was active",
                     pid=None))
        if n:
            log.info("shutdown: aborted %d in-flight run(s)", n)
        return n

    @staticmethod
    def recover_orphans():
        """Runs left 'queued'/'running' by a previous process are dead: this
        is a fresh process, nothing is executing them."""
        stale = Run.objects.filter(status__in=Run.ACTIVE_STATUSES)
        n = stale.update(status=Run.ABORTED, finished_at=timezone.now(),
                         error_message="server restarted while this run was active",
                         pid=None)
        if n:
            log.warning("marked %d orphaned run(s) as aborted", n)

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                run_id = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                execute_run(run_id)
            except Exception:
                log.exception("run %s crashed the worker; marking as error", run_id)
                try:
                    close_old_connections()
                    run = Run.objects.get(pk=run_id)
                    if run.status in Run.ACTIVE_STATUSES:
                        _finalize(run, Run.ERROR, error="internal runner error (see server log)")
                except Exception:
                    pass
            finally:
                self.queue.task_done()

    # -- public API ------------------------------------------------------------
    def enqueue_tests(self, tests, trigger="manual", group=None, label="",
                      browser="", schedule=None) -> Batch:
        cfg = appconfig.get_config()
        tests = [t for t in tests if t.enabled and not t.archived]
        if not tests:
            raise ValueError("nothing to run: no enabled tests in the selection")
        if not label:
            if group is not None:
                label = f"group: {group.name}"
            elif len(tests) == 1:
                label = tests[0].test_id
            else:
                label = f"{len(tests)} tests"
        batch = Batch.objects.create(label=label, trigger=trigger, group=group,
                                     schedule=schedule)
        for test in tests:
            run = Run.objects.create(
                test=test, batch=batch, trigger=trigger,
                target_url=cfg.target_url, target_version=cfg.target_version,
                browser=browser or cfg.browser, headed=cfg.headed,
            )
            self.queue.put(run.pk)
        log.info("enqueued batch %s (%s): %d run(s)", batch.pk, label, len(tests))
        return batch


# ---------------------------------------------------------------------------
# kill -- works with or without a live Runner in this process
# ---------------------------------------------------------------------------

def kill_run(run_id: int) -> bool:
    # Every write here is a CONDITIONAL, single-column update. An earlier
    # version loaded the row, branched on the in-memory copy and then called
    # a bare save(), which raced the worker: a run that had just started
    # could have its started_at/pid/artifacts_rel reset to the stale values
    # (orphaning the artifacts folder), and a run that had just finished
    # could be resurrected as "killed".
    flagged = Run.objects.filter(pk=run_id, status__in=Run.ACTIVE_STATUSES) \
                         .update(kill_requested=True)
    if not flagged:
        return False

    # Still queued? Finalize it here; a worker that picks it up afterwards
    # sees status != queued and skips it.
    if Run.objects.filter(pk=run_id, status=Run.QUEUED).update(
            status=Run.KILLED, finished_at=timezone.now(), pid=None,
            error_message="stopped by request (Kill) before it started"):
        return True

    run = Run.objects.filter(pk=run_id).first()
    if run is None or run.status not in Run.ACTIVE_STATUSES:
        return True   # it finished on its own; the flag is harmless
    if run.pid:
        # Fast path; the executing loop also notices the flag within 2s.
        #
        # Signal the HARNESS ONLY -- never the process group. The harness
        # turns SIGTERM into an exception so its teardown can close the
        # context (which is what finalises video.webm) and write result.json.
        # killpg here hit Chromium at the same instant, so the browser died
        # mid-teardown: measured, a killed run kept NO result.json, NO video,
        # and left a 0-byte file in _video_tmp. The executing loop escalates
        # to the group via _terminate_group() if the harness does not exit.
        try:
            os.kill(run.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    return True


def kill_batch(batch_id: int) -> int:
    killed = 0
    for run in Run.objects.filter(batch_id=batch_id, status__in=Run.ACTIVE_STATUSES):
        if kill_run(run.pk):
            killed += 1
    return killed

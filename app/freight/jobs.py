"""Background jobs for the demo's slow work: route optimization, manifest
import, monthly reports.

They exist to give the hub something that TAKES A WHILE on the server, with
visible progress -- the shape of a real batch process. A page starts a job
and polls /api/jobs/<id>/; a test waits for it and times it.

In memory, in the serving process, on purpose: the hub is ONE process
(waitress + threads), jobs are demo fixtures, and a restart losing a
half-finished demo job is fine. Job threads never touch the ORM (the
connection-hygiene rule in CLAUDE.md): they compute from catalog data only.

At most MAX_RUNNING execute at once; beyond that a job waits, visibly
("waiting for a free worker"), which is realistic too. Everything a poll
returns is copied under the job's lock, so a page never sees half an update.
"""
import collections
import logging
import secrets
import threading
import time

log = logging.getLogger("freight.jobs")

MAX_RUNNING = 3
KEEP = 150                # finished jobs remembered, for their result pages
LOG_LINES = 400
TIME_SCALE = 1.0          # unit tests set 0: job.sleep() returns at once

QUEUED, RUNNING, DONE, FAILED, CANCELLED = (
    "queued", "running", "done", "failed", "cancelled")
FINISHED = (DONE, FAILED, CANCELLED)


class Cancelled(Exception):
    """Raised inside a job when someone pressed Cancel."""


class Job:
    def __init__(self, kind, title, params, version):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.title = title
        self.params = dict(params)
        self.version = version            # the release it ran on
        self.created = time.time()
        self.started = None
        self.finished = None
        self.state = QUEUED
        self.progress = 0.0
        self.stage = "Waiting for a free worker"
        self.result = None
        self.error = ""
        self._log = collections.deque(maxlen=LOG_LINES)
        self._log_seq = 0
        self._series = []
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- called from the worker thread ------------------------------------
    def update(self, progress=None, stage=None):
        with self._lock:
            if progress is not None:
                self.progress = max(0.0, min(1.0, float(progress)))
            if stage is not None:
                self.stage = stage

    def say(self, line):
        with self._lock:
            self._log_seq += 1
            self._log.append((self._log_seq, round(self.elapsed, 1), str(line)))

    def point(self, *values):
        """One data point for the page's live chart."""
        with self._lock:
            self._series.append(list(values))

    def sleep(self, seconds):
        """Pace the work; returns early -- by raising -- on Cancel."""
        if self._cancel.wait(max(0.0, seconds) * TIME_SCALE):
            raise Cancelled()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()

    # -- called from web threads -------------------------------------------
    def cancel(self):
        self._cancel.set()

    @property
    def elapsed(self):
        if not self.started:
            return 0.0
        return (self.finished or time.time()) - self.started

    @property
    def is_finished(self):
        return self.state in FINISHED

    def snapshot(self, log_since=0, series_since=0):
        with self._lock:
            return {
                "id": self.id, "kind": self.kind, "title": self.title,
                "state": self.state, "progress": round(self.progress, 4),
                "percent": int(round(self.progress * 100)),
                "stage": self.stage, "version": self.version,
                "elapsed": round(self.elapsed, 1),
                "error": self.error,
                "log": [{"seq": s, "t": t, "text": x}
                        for s, t, x in self._log if s > log_since],
                "series": self._series[series_since:],
                "series_total": len(self._series),
            }

    def _begin(self):
        with self._lock:
            self.state = RUNNING
            self.started = time.time()
            self.stage = "Starting"

    def _finish(self, state, result=None, error=""):
        with self._lock:
            self.state = state
            self.finished = time.time()
            self.result = result
            self.error = error
            if state == DONE:
                self.progress = 1.0
                self.stage = "Finished"
            elif state == CANCELLED:
                self.stage = "Cancelled"
            else:
                self.stage = "Failed"


_jobs = collections.OrderedDict()
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_RUNNING)


def submit(kind, title, fn, params, version):
    """Start fn(job, params) in its own thread; returns the Job at once."""
    job = Job(kind, title, params, version)
    with _lock:
        _jobs[job.id] = job
        _trim()
    threading.Thread(target=_run, args=(job, fn), daemon=True,
                     name=f"freight-{kind}-{job.id}").start()
    return job


def _run(job, fn):
    acquired = False
    try:
        while not _slots.acquire(timeout=0.25):
            job.check()                  # cancelled while still queued
        acquired = True
        job._begin()
        job.say(f"{job.title} -- started on release {job.version}")
        result = fn(job, job.params)
        job.say(f"finished in {job.elapsed:.1f}s")
        job._finish(DONE, result=result)
    except Cancelled:
        job.say("cancelled")
        job._finish(CANCELLED)
    except Exception as exc:              # a demo job must fail visibly, not vanish
        log.exception("freight job %s (%s) failed", job.id, job.kind)
        job.say(f"failed: {exc}")
        job._finish(FAILED, error=f"{type(exc).__name__}: {exc}")
    finally:
        if acquired:
            _slots.release()


def _trim():
    finished = [j for j in _jobs.values() if j.is_finished]
    for job in finished[:max(0, len(finished) - KEEP)]:
        _jobs.pop(job.id, None)


def get(job_id):
    with _lock:
        return _jobs.get(str(job_id or ""))


def recent(kind=None, limit=8):
    with _lock:
        jobs = [j for j in _jobs.values() if kind is None or j.kind == kind]
    return list(reversed(jobs))[:limit]


def wait(job, timeout=30.0):
    """For tests and scripts: block until the job finishes (or time out)."""
    deadline = time.time() + timeout
    while not job.is_finished and time.time() < deadline:
        time.sleep(0.01)
    return job.is_finished

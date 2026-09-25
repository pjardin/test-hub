"""DB schema.

Ownership split (this is the core design decision):

  FILES own test definitions.   tests/<ID>__name.py + .json sidecar + _groups.json
                                travel on a disk between machines; `sync`
                                re-populates these tables from them at any time.
  DB owns run history.          Runs/Batches accumulate on the machine that
                                executed them and are never derived from files.

JSON-ish values (tags, weekday lists) are stored as JSON text in TextFields
rather than JSONField: JSONField needs the SQLite JSON1 extension, and this
must run on whatever SQLite a hardened government box ended up with.
"""
import json

from django.db import models
from django.utils import timezone

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # 0=Monday


def _loads(text, fallback):
    try:
        val = json.loads(text)
    except (TypeError, ValueError):
        return fallback
    return val if isinstance(val, type(fallback)) else fallback


class Test(models.Model):
    """One Playwright test file. test_id comes from the filename
    (everything before the first '__') and is the stable join key across
    machines, re-syncs and renames."""

    test_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    file_path = models.CharField(max_length=500)  # relative to tests dir
    # Which harness runs it. Derived from the file extension by the syncer
    # (never stored in the sidecar): .py -> the Python harness,
    # .spec.ts -> the TS engine's @playwright/test at /opt/pw-ts.
    TYPE_PYTHON, TYPE_TS = "python", "ts"
    TYPE_CHOICES = [
        (TYPE_PYTHON, "Python (def run(page, ctx))"),
        (TYPE_TS, "TypeScript (@playwright/test)"),
    ]
    test_type = models.CharField(
        max_length=8, choices=TYPE_CHOICES, default=TYPE_PYTHON, blank=True,
    )
    tags_json = models.TextField(default="[]")
    version_tag = models.CharField(
        max_length=100, blank=True,
        help_text="Version of the app under test this test is associated with",
    )
    timeout_seconds = models.PositiveIntegerField(
        default=0, help_text="0 = use the runner default from config.json"
    )
    INHERIT, VIDEO_ON, VIDEO_OFF = "", "on", "off"
    VIDEO_CHOICES = [
        (INHERIT, "use the global setting"),
        (VIDEO_ON, "always record video"),
        (VIDEO_OFF, "never record video"),
    ]
    video_mode = models.CharField(
        max_length=8, choices=VIDEO_CHOICES, default=INHERIT, blank=True,
        help_text="Per-test video override. Turn it off for tests that spend "
                  "most of their time waiting: the .webm grows with wall-clock "
                  "time whenever the page repaints (a countdown, a progress "
                  "bar), so a 30-minute wait costs ~80 MB and shows nothing. "
                  "The activity reel already covers those runs.",
    )
    enabled = models.BooleanField(default=True)
    archived = models.BooleanField(
        default=False, help_text="File disappeared from tests/; history kept"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["test_id"]

    def __str__(self):
        return self.test_id

    @property
    def tags(self):
        return _loads(self.tags_json, [])

    @tags.setter
    def tags(self, value):
        self.tags_json = json.dumps(sorted({str(t).strip() for t in value if str(t).strip()}))

    @property
    def display_name(self):
        return self.name or self.test_id


class Group(models.Model):
    """A named set of tests that run together with one click (or schedule)."""

    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    tests = models.ManyToManyField(Test, related_name="groups", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Schedule(models.Model):
    """Weekly schedule: chosen weekdays at a wall-clock time in the configured
    timezone. Points at either a single test or a group, never both."""

    test = models.ForeignKey(Test, null=True, blank=True, on_delete=models.CASCADE,
                             related_name="schedules")
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="schedules")
    days_json = models.TextField(default="[]")  # [0..6], 0=Monday
    time_of_day = models.TimeField()
    browser = models.CharField(
        max_length=40, blank=True, default="",
        help_text="Browser these scheduled runs simulate on; empty = config default")
    enabled = models.BooleanField(default=True)
    last_fired = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["time_of_day"]

    def __str__(self):
        return f"{self.target_label} @ {self.days_display} {self.time_of_day:%H:%M}"

    @property
    def days(self):
        return sorted(d for d in _loads(self.days_json, []) if isinstance(d, int) and 0 <= d <= 6)

    @days.setter
    def days(self, value):
        self.days_json = json.dumps(sorted({int(d) for d in value if 0 <= int(d) <= 6}))

    @property
    def days_display(self):
        return ",".join(DAY_NAMES[d].capitalize() for d in self.days) or "never"

    @property
    def target_label(self):
        if self.group_id:
            return f"group:{self.group.name}"
        return self.test.test_id if self.test_id else "?"


class Batch(models.Model):
    """One 'run these together' request: a multi-select, a group click, or a
    schedule firing. Single-test runs get a batch too, so killing and progress
    reporting have one shape everywhere."""

    TRIGGER_CHOICES = [
        ("manual", "manual"),        # single test, Run button
        ("selection", "selection"),  # multi-select on the tests page
        ("group", "group"),
        ("schedule", "schedule"),
        ("terminal", "terminal"),    # `testhub run` / `runtest` (since 2.16; older: manual)
    ]

    label = models.CharField(max_length=300, blank=True)
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default="manual")
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.SET_NULL)
    # The schedule that fired this batch (since 2.16): what the Schedules
    # page's history links by. Older scheduled batches only have their label.
    schedule = models.ForeignKey(Schedule, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="batches")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "batches"

    def __str__(self):
        return f"batch {self.pk}: {self.label or self.trigger}"

    @property
    def runs_summary(self):
        counts = {}
        for run in self.runs.all():
            counts[run.status] = counts.get(run.status, 0) + 1
        return counts

    @property
    def is_active(self):
        return self.runs.filter(status__in=Run.ACTIVE_STATUSES).exists()


class Run(models.Model):
    """One execution of one test. The artifacts directory holds everything the
    harness produced: run.log, result.json, screenshots/, video, trace."""

    QUEUED, RUNNING = "queued", "running"
    PASSED, FAILED, ERROR = "passed", "failed", "error"
    TIMEOUT, KILLED, ABORTED = "timeout", "killed", "aborted"

    STATUS_CHOICES = [(s, s) for s in
                      (QUEUED, RUNNING, PASSED, FAILED, ERROR, TIMEOUT, KILLED, ABORTED)]
    ACTIVE_STATUSES = (QUEUED, RUNNING)
    FINISHED_STATUSES = (PASSED, FAILED, ERROR, TIMEOUT, KILLED, ABORTED)
    # Statuses whose duration is a fair sample for ETA math (a killed or
    # crashed run says nothing about how long the test normally takes).
    REPRESENTATIVE_STATUSES = (PASSED, FAILED)

    test = models.ForeignKey(Test, on_delete=models.PROTECT, related_name="runs")
    batch = models.ForeignKey(Batch, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="runs")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=QUEUED)
    trigger = models.CharField(max_length=20, default="manual")

    queued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    target_url = models.CharField(max_length=500, blank=True)
    target_version = models.CharField(max_length=100, blank=True)
    browser = models.CharField(max_length=40, blank=True)
    headed = models.BooleanField(default=False)

    exit_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    artifacts_rel = models.CharField(max_length=600, blank=True)  # under results dir
    pid = models.IntegerField(null=True, blank=True)
    kill_requested = models.BooleanField(default=False)
    # per-step timings from the harness: [{name, op, detail, ms, t}, ...]
    timings_json = models.TextField(default="[]", blank=True)
    # artifacts offloaded to object storage (see services/storage.py)
    # Load testing: an iteration belongs to a LoadRun and knows its index.
    load_run = models.ForeignKey("LoadRun", null=True, blank=True,
                                 on_delete=models.CASCADE, related_name="iterations")
    iteration = models.PositiveIntegerField(default=0)
    # Load iterations record nothing by default: 250 videos of the same test
    # is gigabytes of disk to say what the timings already say.
    minimal_artifacts = models.BooleanField(default=False)

    # Passive security observations from the run (see harness/security.py).
    security_json = models.TextField(default="{}", blank=True)

    artifacts_remote = models.BooleanField(default=False)
    artifacts_index_json = models.TextField(default="[]", blank=True)

    class Meta:
        ordering = ["-queued_at"]
        indexes = [
            models.Index(fields=["test", "status"]),
            models.Index(fields=["test", "-finished_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.test.test_id} #{self.pk} [{self.status}]"

    @property
    def security(self):
        import json as _json
        try:
            return _json.loads(self.security_json or "{}")
        except ValueError:
            return {}

    @staticmethod
    def regular():
        """Runs a person asked for -- EXCLUDING load-test iterations.

        This is what every chart, ETA and pass-rate should read. A single
        10-minute load test adds ~1300 iterations, so without this one load
        run buries a test's real history: measured on the demo test, 99% of
        its runs were load iterations and the ETA shown for a normal run had
        become the LOADED duration (1.38s instead of 1.02s). Load runs have
        their own page with their own numbers; they do not belong in these.
        """
        return Run.objects.filter(load_run__isnull=True)

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def ok(self):
        return self.status == self.PASSED

    @property
    def timings(self):
        return _loads(self.timings_json, [])

    @property
    def elapsed_seconds(self):
        if self.duration_seconds is not None:
            return self.duration_seconds
        if self.started_at and self.status == self.RUNNING:
            return (timezone.now() - self.started_at).total_seconds()
        return None


# ---------------------------------------------------------------------------
# Load / performance testing
# ---------------------------------------------------------------------------

class LoadPlan(models.Model):
    """A saved load test: take ONE existing test and run it over and over,
    several at a time, for a fixed stretch of wall-clock time.

    Deliberately built on the ordinary test + runner rather than a separate
    engine: every iteration is a real browser driving the real application,
    and it already records per-step timings -- which is what makes it
    possible to say WHICH step degrades under load, not just that the total
    got slower.
    """

    name = models.CharField(max_length=200)
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name="load_plans")
    description = models.TextField(blank=True)

    duration_seconds = models.PositiveIntegerField(
        default=600, help_text="How long to keep applying load (wall clock).")
    concurrency = models.PositiveIntegerField(
        default=3, help_text="How many copies of the test run at the same time "
                             "-- each one is a real browser, so this is bounded "
                             "by the machine's RAM and CPU, not by ambition.")
    ramp_seconds = models.PositiveIntegerField(
        default=0, help_text="Start the copies gradually over this many seconds, "
                             "so you can see where it starts to hurt.")
    think_time_seconds = models.FloatField(
        default=0, help_text="Pause between iterations, imitating a human "
                             "reading the page.")
    max_iterations = models.PositiveIntegerField(
        default=0, help_text="0 = no cap; stop only when the time is up.")

    browser = models.CharField(max_length=40, blank=True)
    keep_artifacts = models.BooleanField(
        default=False,
        help_text="Off by default. Hundreds of videos of the same test is "
                  "gigabytes of disk to repeat what the timings already say.")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class LoadRun(models.Model):
    """One execution of a load plan."""

    QUEUED, RUNNING, FINISHED, KILLED, ERROR = (
        "queued", "running", "finished", "killed", "error")
    STATUS_CHOICES = [(s, s) for s in (QUEUED, RUNNING, FINISHED, KILLED, ERROR)]
    ACTIVE_STATUSES = (QUEUED, RUNNING)

    plan = models.ForeignKey(LoadPlan, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="runs")
    test = models.ForeignKey(Test, on_delete=models.PROTECT, related_name="load_runs")
    label = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=QUEUED)

    # the settings this execution actually used (the plan can change later)
    duration_seconds = models.PositiveIntegerField(default=600)
    concurrency = models.PositiveIntegerField(default=3)
    ramp_seconds = models.PositiveIntegerField(default=0)
    think_time_seconds = models.FloatField(default=0)
    max_iterations = models.PositiveIntegerField(default=0)
    browser = models.CharField(max_length=40, blank=True)
    keep_artifacts = models.BooleanField(default=False)

    # what the plan was based on, kept so a later reader can judge it
    baseline_seconds = models.FloatField(null=True, blank=True)
    baseline_source = models.CharField(max_length=40, blank=True)
    projected_iterations = models.PositiveIntegerField(default=0)

    target_url = models.CharField(max_length=500, blank=True)
    target_version = models.CharField(max_length=100, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    kill_requested = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    summary_json = models.TextField(default="{}", blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"load run {self.pk}: {self.label or self.test_id}"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def elapsed_seconds(self):
        if not self.started_at:
            return None
        end = self.finished_at or timezone.now()
        return (end - self.started_at).total_seconds()

    @property
    def summary(self):
        import json as _json
        try:
            return _json.loads(self.summary_json or "{}")
        except ValueError:
            return {}

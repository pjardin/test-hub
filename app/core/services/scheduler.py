"""Weekly scheduler: fire tests/groups on chosen weekdays at a wall-clock
time in the configured timezone (default America/Los_Angeles).

A schedule fires when:
  * today (in the schedule timezone) is one of its weekdays, and
  * the wall-clock slot time has passed, and
  * we are still within the catch-up window (so a server that was down at
    09:00 and comes back at 09:20 still runs the 09:00 suite, but one that
    comes back at 23:00 does not), and
  * it has not already fired for this slot (last_fired watermark).

The loop ticks every 20 seconds; firing precision is therefore ~20s, which
is plenty for a nightly/weekly regression cadence.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db import close_old_connections
from django.utils import timezone

from core import appconfig
from core.models import Schedule

log = logging.getLogger("testhub.scheduler")

TICK_SECONDS = 20
MIN_WINDOW_MINUTES = 5  # even with catchup disabled a slot stays live this long


def due_slot(schedule, now_utc, tzname, catchup_minutes) -> "datetime | None":
    """Pure function (unit-tested): the UTC slot datetime if `schedule` should
    fire at `now_utc`, else None.

    Two subtleties, both found by review rather than in the field:

    * YESTERDAY's slot is considered as well as today's. A 23:30 job with a
      60-minute catch-up window used to be lost entirely if the hub was
      restarted at 23:25 and came back at 00:05 -- by then "today" is the
      next day and its 23:30 is still in the future.
    * All comparisons are done in UTC. `datetime.combine(date, time, tz)`
      does not normalise, and because both sides share one ZoneInfo, Python
      compares them as naive wall-clock -- which on a spring-forward day
      makes a 02:30 slot fire 30 minutes late, or never with a short window.
    """
    if not schedule.enabled or not schedule.days:
        return None
    tz = ZoneInfo(tzname)
    now_local = now_utc.astimezone(tz)
    window = timedelta(minutes=max(catchup_minutes, MIN_WINDOW_MINUTES))

    # newest first: today's slot, then yesterday's (the midnight case)
    for day in (now_local.date(), now_local.date() - timedelta(days=1)):
        if day.weekday() not in schedule.days:
            continue
        slot_utc = datetime.combine(day, schedule.time_of_day,
                                    tzinfo=tz).astimezone(dt_timezone.utc)
        if now_utc < slot_utc:
            continue                      # not due yet
        if now_utc - slot_utc > window:
            continue                      # missed it by too much
        if schedule.last_fired and schedule.last_fired >= slot_utc:
            continue                      # already fired for this slot
        return slot_utc
    return None


def next_slot(schedule, now_utc, tzname) -> "datetime | None":
    """The next UTC datetime this schedule will fire (display only)."""
    if not schedule.enabled or not schedule.days:
        return None
    tz = ZoneInfo(tzname)
    now_local = now_utc.astimezone(tz)
    for offset in range(0, 8):
        day = now_local.date() + timedelta(days=offset)
        if day.weekday() not in schedule.days:
            continue
        slot_local = datetime.combine(day, schedule.time_of_day, tzinfo=tz)
        if slot_local <= now_local:
            continue
        return slot_local.astimezone(dt_timezone.utc)
    return None


class SchedulerThread(threading.Thread):
    def __init__(self, runner):
        super().__init__(name="scheduler", daemon=True)
        self.runner = runner
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info("scheduler started (timezone %s)", appconfig.get_config().timezone)
        while not self.stop_event.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed; continuing")

    def tick(self):
        close_old_connections()
        cfg = appconfig.get_config()
        now_utc = timezone.now()
        for sched in Schedule.objects.filter(enabled=True).select_related("test", "group"):
            slot = due_slot(sched, now_utc, cfg.timezone, cfg.catchup_minutes)
            if slot is None:
                continue
            self.fire(sched, slot)

    def fire(self, sched, slot_utc):
        # Watermark FIRST: if enqueueing explodes we log it, we do not
        # re-fire the same slot every 20 seconds.
        sched.last_fired = slot_utc
        sched.save(update_fields=["last_fired"])
        try:
            if sched.group_id:
                tests = list(sched.group.tests.filter(enabled=True, archived=False))
                label = f"schedule: {sched.group.name} {sched.time_of_day:%H:%M}"
                group = sched.group
            else:
                tests = [sched.test]
                label = f"schedule: {sched.test.test_id} {sched.time_of_day:%H:%M}"
                group = None
            self.runner.enqueue_tests(tests, trigger="schedule", group=group,
                                      label=label, browser=sched.browser, schedule=sched)
            log.info("schedule fired: %s", label)
        except ValueError as exc:
            log.warning("schedule %s fired but enqueued nothing: %s", sched.pk, exc)

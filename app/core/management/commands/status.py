"""Is the hub serving? What is it doing right now?"""
from django.core.management.base import BaseCommand

from core.models import Run, Schedule, Test
from core.services import terminal


class Command(BaseCommand):
    help = "Hub service state, activity and totals"

    def handle(self, *args, **opts):
        st = terminal.service_state()
        if st["running"]:
            self.stdout.write(f"hub service : SERVING on port {st['port']}")
        else:
            self.stdout.write("hub service : NOT SERVING -- queued runs and schedules wait")
            self.stdout.write("              start it: sudo systemctl start pw-testhub"
                              "   (or: testhub serve)")
        act = terminal.active_runs()
        running = [r for r in act if r.status == Run.RUNNING]
        queued = [r for r in act if r.status == Run.QUEUED]
        self.stdout.write(f"activity    : {len(running)} running, {len(queued)} queued")
        for r in running:
            self.stdout.write(f"    #{r.pk} {r.test.test_id} running")
        self.stdout.write(
            "tests       : "
            f"{Test.objects.filter(archived=False).count()} on disk "
            f"({Test.objects.filter(archived=False, test_type='ts').count()} TypeScript), "
            f"{Schedule.objects.count()} schedule(s)")
        recent = Run.regular().order_by("-id")[:5]
        if recent:
            self.stdout.write("recent      :")
            for r in recent:
                self.stdout.write(f"    #{r.pk} {terminal.pad(r.test.test_id, 14)}"
                                  f"{terminal.pad(r.status, 9)}"
                                  f"{terminal.fmt_dur(r.duration_seconds)}")

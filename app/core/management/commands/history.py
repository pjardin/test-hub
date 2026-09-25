"""Recent runs of one test."""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Run, Test
from core.services import terminal


class Command(BaseCommand):
    help = "Recent runs for a test"

    def add_arguments(self, parser):
        parser.add_argument("test_id")
        parser.add_argument("count", nargs="?", type=int, default=15)

    def handle(self, *args, **opts):
        try:
            test = Test.objects.get(test_id=opts["test_id"])
        except Test.DoesNotExist:
            raise CommandError(f"no test '{opts['test_id']}'")
        p = terminal.pad
        for run in Run.regular().filter(test=test).order_by("-id")[:opts["count"]]:
            when = (timezone.localtime(run.finished_at).strftime("%Y-%m-%d %H:%M")
                    if run.finished_at else "")
            first = (run.error_message or "").splitlines()[0][:60] if run.error_message else ""
            self.stdout.write(p(f"#{run.pk}", 8) + p(run.status, 9)
                              + p(terminal.fmt_dur(run.duration_seconds), 9)
                              + p(when, 18) + first)

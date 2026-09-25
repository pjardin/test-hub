"""The Stop button, terminal edition -- works cross-process via the DB flag."""
from django.core.management.base import BaseCommand, CommandError

from core.models import Run
from core.services import terminal


class Command(BaseCommand):
    help = "Stop a queued or running run"

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)

    def handle(self, *args, **opts):
        run = Run.objects.filter(pk=opts["run_id"]).first()
        if not run:
            raise CommandError(f"no run {opts['run_id']}")
        if run.status not in Run.ACTIVE_STATUSES:
            self.stdout.write(f"run {run.pk} is already {run.status}")
            return
        terminal.kill_from_terminal(run.pk)
        self.stdout.write(f"kill requested for run {run.pk} -- the hub stops it within seconds")

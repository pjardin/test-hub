"""Queue tests into the SERVING hub and exit -- the terminal is free, the
run continues after logout. (Sync-in-this-terminal runs: `testhub runtest`.)"""
from django.core.management.base import BaseCommand, CommandError

from core.models import Test
from core.services import terminal


class Command(BaseCommand):
    help = "Queue tests into the running hub service and exit immediately"

    def add_arguments(self, parser):
        parser.add_argument("test_ids", nargs="+")
        parser.add_argument("--browser", default="",
                            help="chromium/chrome/msedge (TS tests) or any python-hub browser")

    def handle(self, *args, **opts):
        tests = []
        for tid in opts["test_ids"]:
            try:
                tests.append(Test.objects.get(test_id=tid, archived=False))
            except Test.DoesNotExist:
                raise CommandError(f"no test '{tid}' (testhub list)")
        try:
            runs = terminal.enqueue_cli(tests, browser=opts["browser"])
        except ValueError as exc:
            raise CommandError(str(exc))
        for run in runs:
            self.stdout.write(f"{run.test.test_id}: queued as run {run.pk}"
                              f"   (follow it: testhub watch {run.pk})")
        terminal.warn_if_no_service(self.stdout.write)
        self.stdout.write("you can log out now -- the hub service owns the run from here.")

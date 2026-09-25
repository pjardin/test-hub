"""Scaffold a test: python by default, --ts for a TypeScript spec that runs
under the TS engine's @playwright/test."""
from django.core.management.base import BaseCommand, CommandError

from core.models import Test
from core.services import terminal
from core.services.syncer import TestFileError


class Command(BaseCommand):
    help = 'Scaffold a new test: testhub new PAY-001 "Refund flow" [--ts]'

    def add_arguments(self, parser):
        parser.add_argument("test_id")
        parser.add_argument("name", nargs="*")
        parser.add_argument("--ts", action="store_true",
                            help="TypeScript (@playwright/test) instead of python")

    def handle(self, *args, **opts):
        test_id = opts["test_id"]
        if Test.objects.filter(test_id=test_id).exists():
            raise CommandError(f"{test_id} already exists")
        name = " ".join(opts["name"]).strip() or test_id
        try:
            filename = terminal.scaffold(test_id, name, ts=opts["ts"])
        except FileExistsError as exc:
            raise CommandError(f"file already exists: {exc}")
        except TestFileError as exc:  # invalid id -- same rules as the web form
            raise CommandError(str(exc))
        self.stdout.write(f"created {filename}")
        self.stdout.write(f"    edit it:  testhub edit {test_id}")
        self.stdout.write(f"    run it:   testhub run {test_id}")

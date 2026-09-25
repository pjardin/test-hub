"""Open a test in $EDITOR (vi by default), then rescan."""
from django.core.management.base import BaseCommand, CommandError

from core.models import Test
from core.services import terminal


class Command(BaseCommand):
    help = "Edit a test file in $EDITOR, then rescan"

    def add_arguments(self, parser):
        parser.add_argument("test_id")

    def handle(self, *args, **opts):
        try:
            test = Test.objects.get(test_id=opts["test_id"])
        except Test.DoesNotExist:
            raise CommandError(f"no test '{opts['test_id']}'")
        summary = terminal.open_in_editor(test)
        errs = summary.get("errors") or []
        self.stdout.write("rescanned after edit"
                          + (f" -- problems: {'; '.join(errs)}" if errs else ""))

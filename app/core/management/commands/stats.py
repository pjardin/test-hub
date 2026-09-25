"""Pass rates and honest medians (Run.regular(): load iterations excluded)."""
from django.core.management.base import BaseCommand, CommandError

from core.models import Test
from core.services import terminal


class Command(BaseCommand):
    help = "Per-test statistics: runs, pass rate, median duration"

    def add_arguments(self, parser):
        parser.add_argument("test_id", nargs="?")

    def handle(self, *args, **opts):
        rows = terminal.test_rows()
        if opts["test_id"]:
            if not Test.objects.filter(test_id=opts["test_id"]).exists():
                raise CommandError(f"no test '{opts['test_id']}'")
            rows = [r for r in rows if r["test"].test_id == opts["test_id"]]
        p = terminal.pad
        self.stdout.write(p("ID", 14) + p("RUNS", 6) + p("PASS%", 7)
                          + p("MEDIAN", 9) + "LAST")
        for r in rows:
            self.stdout.write(
                p(r["test"].test_id, 14) + p(r["runs"], 6)
                + p("-" if r["pass_rate"] is None else f"{r['pass_rate']}%", 7)
                + p(terminal.fmt_dur(r["median_s"]), 9)
                + (r["last_status"] or "-"))

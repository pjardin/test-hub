"""The tests table, in a terminal."""
from django.core.management.base import BaseCommand

from core.services import terminal


class Command(BaseCommand):
    help = "List tests with last result, pass rate and schedules"

    def handle(self, *args, **opts):
        rows = terminal.test_rows()
        if not rows:
            self.stdout.write("no tests yet. Scaffold one: testhub new ID \"Name\" [--ts]")
            return
        p = terminal.pad
        self.stdout.write(p("ID", 14) + p("TYPE", 8) + p("NAME", 30)
                          + p("RUNS", 6) + p("PASS%", 7) + p("SCHED", 7) + "LAST")
        for r in rows:
            t = r["test"]
            last = f"{r['last_status']} ({r['last_when']})" if r["last_status"] else "never ran"
            if not t.enabled:
                last += "  [disabled]"
            self.stdout.write(
                p(t.test_id, 14) + p(t.test_type, 8) + p(t.display_name, 30)
                + p(r["runs"], 6)
                + p("-" if r["pass_rate"] is None else f"{r['pass_rate']}%", 7)
                + p(r["schedules"] or "-", 7) + last)
        terminal.warn_if_no_service(self.stdout.write)

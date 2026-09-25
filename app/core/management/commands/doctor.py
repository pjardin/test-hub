"""Self-diagnosis from the command line — works even when the web UI will
not start, which is exactly when you need it.

    testhub doctor          full check (launches a browser)
    testhub doctor --quick  skip the browser launch
"""
from django.core.management.base import BaseCommand

from core.services import diagnostics


class Command(BaseCommand):
    help = "Check this machine: python, browsers, database, disk, target, schedules"

    def add_arguments(self, parser):
        parser.add_argument("--quick", action="store_true",
                            help="skip the browser launch check")

    def handle(self, *args, **opts):
        checks = diagnostics.run_checks(quick=opts["quick"])
        width = max(len(c["name"]) for c in checks)
        for c in checks:
            mark = {"ok": "  OK  ", "warn": " WARN ", "fail": " FAIL "}[c["status"]]
            style = {"ok": self.style.SUCCESS, "warn": self.style.WARNING,
                     "fail": self.style.ERROR}[c["status"]]
            self.stdout.write(f"[{style(mark)}] {c['name']:<{width}}  {c['detail']}")
            if c["fix"]:
                self.stdout.write(f"          -> {c['fix']}")
        s = diagnostics.summary(checks)
        self.stdout.write("")
        line = f"{s['ok']} ok, {s['warn']} warning(s), {s['fail']} failure(s)"
        self.stdout.write(self.style.SUCCESS(line) if s["healthy"]
                          else self.style.ERROR(line))
        raise SystemExit(0 if s["healthy"] else 1)

"""Schedule a test from the terminal. The schedule is written into the
test's sidecar (files own definitions, so it travels with the file) and
fires inside the serving hub -- nobody has to be logged in."""
from django.core.management.base import BaseCommand, CommandError

from core.models import Schedule, Test
from core.services import terminal


class Command(BaseCommand):
    help = ("Add a schedule: testhub schedule ID \"mon 02:00\" | \"daily 14:30\" "
            "| ID --clear | no args to list all")

    def add_arguments(self, parser):
        parser.add_argument("test_id", nargs="?")
        parser.add_argument("spec", nargs="?")
        parser.add_argument("--clear", action="store_true")

    def handle(self, *args, **opts):
        if not opts["test_id"]:
            scheds = Schedule.objects.filter(test__isnull=False) \
                .select_related("test").order_by("test__test_id")
            if not scheds:
                self.stdout.write('no schedules. Add one: testhub schedule DEMO-001 "mon 02:00"')
                return
            p = terminal.pad
            for s in scheds:
                days = ",".join(terminal.DAY_KEYS[d] for d in s.days)
                state = "" if s.enabled else "  [disabled]"
                self.stdout.write(p(s.test.test_id, 14)
                                  + p(f"{days} {s.time_of_day.strftime('%H:%M')}", 34) + state)
            terminal.warn_if_no_service(self.stdout.write)
            return
        try:
            test = Test.objects.get(test_id=opts["test_id"])
        except Test.DoesNotExist:
            raise CommandError(f"no test '{opts['test_id']}'")
        if opts["clear"]:
            terminal.clear_schedules(test)
            self.stdout.write(f"{test.test_id}: schedules cleared (sidecar updated)")
            return
        entry = terminal.add_schedule(test, opts["spec"] or "")
        if entry is None:
            raise CommandError(f'"{opts["spec"]}" is not a schedule -- '
                               'want "mon 02:00" or "daily 14:30"')
        self.stdout.write(f"{test.test_id}: will run {opts['spec']} "
                          "(saved to the sidecar, fires inside the serving hub)")
        terminal.warn_if_no_service(self.stdout.write)

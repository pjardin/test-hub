"""Run tests from the command line, synchronously -- no server needed.

    python manage.py runtest DEMO-001
    python manage.py runtest DEMO-001 DEMO-002       # sequentially
    python manage.py runtest --group Smoke

Exit code: 0 if every run passed, 1 otherwise. Used by the offline bundle
verification and handy for cron-style automation outside the built-in
scheduler.
"""
from django.core.management.base import BaseCommand, CommandError

from core import appconfig
from core.models import Batch, Group, Run, Test
from core.services.runner import execute_run
from core.services.syncer import sync_from_files


class Command(BaseCommand):
    help = "Run one or more tests synchronously from the CLI"

    def add_arguments(self, parser):
        parser.add_argument("test_ids", nargs="*", help="test ids to run")
        parser.add_argument("--group", default="", help="run every enabled test in this group")
        parser.add_argument("--no-sync", action="store_true",
                            help="skip the tests-folder sync before running")

    def handle(self, *args, **opts):
        if not opts["no_sync"]:
            sync_from_files()

        tests = []
        group = None
        if opts["group"]:
            try:
                group = Group.objects.get(name=opts["group"])
            except Group.DoesNotExist:
                raise CommandError(f"no group named {opts['group']!r}")
            tests = list(group.tests.filter(enabled=True, archived=False))
        for tid in opts["test_ids"]:
            try:
                tests.append(Test.objects.get(test_id=tid, archived=False))
            except Test.DoesNotExist:
                raise CommandError(f"no test with id {tid!r}")
        if not tests:
            raise CommandError("nothing to run (pass test ids or --group)")

        cfg = appconfig.get_config()
        # Deliberately NOT calling Runner.recover_orphans() here: this command
        # is documented for cron use, and a hub may well be serving on the
        # same machine. Orphan recovery assumes "this is a fresh process, so
        # nothing is executing" -- true at `serve` startup, false here, where
        # it would abort every run the live server has queued. (Measured: a
        # single CLI invocation aborted two in-flight runs.)
        batch = Batch.objects.create(label=f"cli: {len(tests)} test(s)", trigger="terminal",
                                     group=group)
        failures = 0
        for test in tests:
            run = Run.objects.create(
                test=test, batch=batch, trigger="manual",
                target_url=cfg.target_url, target_version=cfg.target_version,
                browser=cfg.browser, headed=cfg.headed,
            )
            self.stdout.write(f"==> {test.test_id}: running ...")
            execute_run(run.pk)
            run.refresh_from_db()
            line = (f"    {test.test_id}: {run.status}"
                    f" ({run.duration_seconds:.1f}s)" if run.duration_seconds
                    else f"    {test.test_id}: {run.status}")
            if run.status == Run.PASSED:
                self.stdout.write(self.style.SUCCESS(line))
            else:
                failures += 1
                self.stdout.write(self.style.ERROR(line))
                if run.error_message:
                    self.stdout.write("      " + run.error_message.splitlines()[0][:200])
                self.stdout.write(f"      artifacts: {cfg.results_dir / run.artifacts_rel}")

        raise SystemExit(1 if failures else 0)

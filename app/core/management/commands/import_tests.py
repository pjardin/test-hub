"""Import test definitions from a tests zip, from the command line.

    python manage.py import_tests /media/disc/tests-20260803.zip
    testhub import_tests /media/disc/tests.zip --no-overwrite

The disc workflow's other half: `export_tests` writes the zip on the laptop,
this reads it at work. The UI can do the same thing (Settings -> Import), but
on a machine that is awkward to reach a browser is not always the easy path,
and the CLI is what a runbook can call.

Only ever touches the tests folder -- run history is never involved -- and it
refuses anything that is not a plain .py/.json pair, so a wrong file cannot
scribble over the machine.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.services.backup import import_tests
from core.services.syncer import sync_from_files


class Command(BaseCommand):
    help = "Import test definitions from a tests zip"

    def add_arguments(self, parser):
        parser.add_argument("zip_path")
        parser.add_argument("--no-overwrite", action="store_true",
                            help="keep existing files instead of replacing them")
        parser.add_argument("--no-sync", action="store_true",
                            help="do not repopulate the database afterwards")

    def handle(self, *args, **opts):
        path = Path(opts["zip_path"])
        if not path.exists():
            raise CommandError(f"no such file: {path}")

        report = import_tests(path, overwrite=not opts["no_overwrite"])

        for name in report["written"]:
            self.stdout.write(self.style.SUCCESS(f"    wrote   {name}"))
        for name in report["skipped"]:
            self.stdout.write(f"    kept    {name} (already there)")
        for problem in report["errors"]:
            self.stdout.write(self.style.WARNING(f"    ignored {problem}"))

        if not opts["no_sync"]:
            summary = sync_from_files()
            self.stdout.write(
                f"\n  {len(summary['created'])} new, {len(summary['updated'])} "
                f"updated in the database")

        self.stdout.write(self.style.SUCCESS(
            f"\n  imported {len(report['written'])} file(s) from {path.name}"))
        if report["errors"]:
            self.stdout.write("  (ignored entries are listed above -- a tests "
                              "zip should hold only .py/.json pairs)")

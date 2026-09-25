"""Write test definitions to a zip, from the command line.

    python manage.py export_tests --out /media/disc/tests.zip
    testhub export_tests --out tests.zip --test DEMO-001 --test DEMO-002

The disc workflow's first half. Carries test definitions ONLY -- never run
history -- so importing it on another machine cannot damage that machine's
records.
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from core.services.backup import export_tests


class Command(BaseCommand):
    help = "Export test definitions to a zip for carrying to another machine"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="tests-export.zip",
                            help="where to write the zip")
        parser.add_argument("--test", action="append", dest="tests",
                            help="only this test id (repeatable; default: all)")

    def handle(self, *args, **opts):
        blob = export_tests(opts.get("tests") or None)
        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)
        self.stdout.write(self.style.SUCCESS(
            f"  wrote {out}  ({len(blob) / 1024:.0f} KB)"))
        self.stdout.write("  Test definitions only -- no run history travels "
                          "in this file.")

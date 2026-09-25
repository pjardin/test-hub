"""Back up everything that would hurt to lose.

    testhub backup                      -> data/backups/backup-manual-<stamp>.zip
    testhub backup --out /media/usb/x.zip
    testhub backup --with-results       (large: includes videos/traces)

Contains the tests folder, the database (run history) and config.json.
Restore with `testhub restore <file>`.
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from core.services import backup as backup_svc


class Command(BaseCommand):
    help = "Back up tests, run history and config into one zip"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="", help="write the zip here")
        parser.add_argument("--with-results", action="store_true",
                            help="also include run artifacts (videos, traces)")

    def handle(self, *args, **opts):
        if opts["out"]:
            path = Path(opts["out"]).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(backup_svc.build_backup(
                include_results=opts["with_results"]))
        else:
            path = backup_svc.auto_backup(reason="manual")
        size = path.stat().st_size / 1e6
        self.stdout.write(self.style.SUCCESS(
            f"backup written: {path}  ({size:.1f} MB)"))

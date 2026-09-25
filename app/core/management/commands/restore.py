"""Restore a backup made by `testhub backup`.

    testhub restore /path/backup.zip              tests + database
    testhub restore /path/backup.zip --tests-only keep this machine's history

Whatever is replaced is MOVED ASIDE (data/_replaced-<stamp>/), never
deleted, so a wrong restore is undoable.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.services import backup as backup_svc
from core.services.syncer import sync_from_files


class Command(BaseCommand):
    help = "Restore tests (and optionally run history) from a backup zip"

    def add_arguments(self, parser):
        parser.add_argument("zip_path")
        parser.add_argument("--tests-only", action="store_true",
                            help="do not replace the database")

    def handle(self, *args, **opts):
        path = Path(opts["zip_path"]).expanduser()
        if not path.exists():
            raise CommandError(f"no such file: {path}")
        self.stdout.write("==> taking a safety backup of the CURRENT state first")
        safety = backup_svc.auto_backup(reason="before-restore")
        self.stdout.write(f"    {safety}")
        try:
            report = backup_svc.restore_backup(path, restore_db=not opts["tests_only"])
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"    restored {report['tests']} test file(s)"
                          + (", database replaced" if report["db"] else ""))
        self.stdout.write(f"    previous state kept at {report['kept_at']}")
        if not report["db"]:
            sync_from_files()
        self.stdout.write(self.style.SUCCESS(
            "restore complete — restart the hub (systemctl restart pw-testhub)"))

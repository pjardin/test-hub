from django.core.management.base import BaseCommand

from core.services.syncer import sync_from_files


class Command(BaseCommand):
    help = "Re-populate the database from the tests/ folder (files win)"

    def handle(self, *args, **opts):
        summary = sync_from_files()
        self.stdout.write(f"created:  {', '.join(summary['created']) or '-'}")
        self.stdout.write(f"updated:  {len(summary['updated'])} test(s)")
        self.stdout.write(f"archived: {', '.join(summary['archived']) or '-'}")
        self.stdout.write(f"restored: {', '.join(summary['restored']) or '-'}")
        self.stdout.write(f"groups:   {summary['groups']}")
        for err in summary["errors"]:
            self.stderr.write(self.style.WARNING(f"warning: {err}"))

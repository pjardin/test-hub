"""Full-screen terminal UI: the hub dashboard over SSH."""
from django.core.management.base import BaseCommand

from core.services.tui import run_tui


class Command(BaseCommand):
    help = "Full-screen terminal UI (run/kill/schedule/edit without a browser)"

    def handle(self, *args, **opts):
        run_tui()

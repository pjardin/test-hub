"""Keep this machine awake from the hub's own launcher.

    testhub caffeinate            hold it awake until Ctrl-C
    testhub caffeinate --status   what would put this machine to sleep?
    testhub caffeinate -t 3600    hold it awake for an hour

Thin passthrough to the `caffeinate` shipped in the base bundle, so there is
one implementation and one behaviour whether you reach it from the hub's
launcher or straight off the PATH.
"""
import os
import shutil
import subprocess
import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Keep this machine awake (wraps the bundle's caffeinate)"

    def add_arguments(self, parser):
        parser.add_argument("args", nargs="*",
                            help="passed straight through to caffeinate")

    def run_from_argv(self, argv):
        # Bypass Django's parser entirely: it would eat -t and --status.
        self._passthrough(argv[2:])

    def handle(self, *args, **opts):
        self._passthrough(list(opts.get("args") or []))

    def _passthrough(self, argv):
        binary = shutil.which("caffeinate")
        if not binary:
            prefix = os.environ.get("PW_OFFLINE_PREFIX", "/opt/pw-offline")
            candidate = os.path.join(prefix, "bin", "caffeinate")
            binary = candidate if os.path.exists(candidate) else None
        if not binary:
            self.stderr.write(
                "caffeinate is not installed. It ships with the base bundle at "
                "$PW_OFFLINE_PREFIX/bin/caffeinate -- re-run the base "
                "install.sh if it is missing.")
            raise SystemExit(2)
        raise SystemExit(subprocess.call([binary] + list(argv)))

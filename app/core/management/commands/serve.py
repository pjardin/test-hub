"""THE way to run the Test Hub.

    python manage.py serve

One process does everything: web UI (waitress), the run queue with its
worker pool, and the weekly scheduler. That single-process shape is
deliberate -- views talk to the runner directly, kills are instant, and
there is no message broker to install on an air-gapped machine.
"""
import signal

from django.core.management import call_command
from django.core.management.base import BaseCommand

from core import appconfig
from core.services import runner as runner_mod
from core.services.runner import Runner
from core.services.scheduler import SchedulerThread
from core.services.syncer import sync_from_files


class Command(BaseCommand):
    help = "Run the Test Hub: web UI + test runner + scheduler in one process"

    def add_arguments(self, parser):
        parser.add_argument("--host", default=None, help="override config site.host")
        parser.add_argument("--port", type=int, default=None, help="override config site.port")
        parser.add_argument("--no-scheduler", action="store_true",
                            help="do not fire weekly schedules from this process")

    def handle(self, *args, **opts):
        try:
            from waitress import serve as waitress_serve
        except ImportError:
            self.stderr.write("waitress is not installed (it ships in the app "
                              "bundle wheelhouse; pip install waitress for dev)")
            raise SystemExit(2)

        cfg = appconfig.get_config()
        host = opts["host"] or cfg.site_host
        port = opts["port"] or cfg.site_port

        from core.services.backup import backup_before_migrating
        saved = backup_before_migrating()
        if saved:
            self.stdout.write(f"==> safety backup before migrating: {saved}")
        self.stdout.write("==> applying database migrations")
        call_command("migrate", interactive=False, verbosity=0)

        self.stdout.write("==> syncing tests folder -> database")
        summary = sync_from_files()
        self.stdout.write(f"    {len(summary['created'])} new, "
                          f"{len(summary['updated'])} updated, "
                          f"{len(summary['archived'])} archived, "
                          f"{summary['groups']} group(s)")
        for err in summary["errors"]:
            self.stderr.write(f"    sync warning: {err}")

        from core.services.housekeeping import startup_prune
        startup_prune()

        runner = runner_mod.install(Runner(cfg))
        runner.start()

        # Load runs left active by a previous process are dead too -- same
        # reasoning as Runner.recover_orphans(), and safe for the same reason:
        # this is a fresh process, so nothing is executing.
        from core.services.loadtest import recover_orphans as recover_load_orphans
        recover_load_orphans()

        if not opts["no_scheduler"]:
            SchedulerThread(runner).start()

        self.stdout.write(self.style.SUCCESS(
            f"\n  Test Hub ready:  http://{host}:{port}/\n"
            f"  target under test: {cfg.target_url}"
            f"{'  (built-in demo page)' if cfg.target_is_demo else ''}\n"
            f"  data dir: {cfg.data_dir}\n"))

        # threads=16: pages are cheap, but live-status polling from a few
        # open tabs plus artifact downloads should never starve the UI.
        # systemd stops this service with SIGTERM, and Python's DEFAULT
        # disposition for SIGTERM is to die immediately -- no finally, no
        # cleanup, so `systemctl restart` orphaned every in-flight harness
        # and its Chromium. Turning it into an exception lets the shutdown
        # below actually run. (Ctrl-C already raises KeyboardInterrupt.)
        def _on_term(signum, _frame):
            raise KeyboardInterrupt(f"signal {signum}")

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _on_term)
            except ValueError:
                pass          # not the main thread (dev embedding); harmless

        try:
            waitress_serve(self._wsgi(), host=host, port=port, threads=16,
                           ident="pw-testhub")
        except KeyboardInterrupt:
            self.stdout.write("\n==> shutting down")
        finally:
            # Without this, Ctrl-C (or `systemctl stop`) leaves every
            # in-flight harness -- and its Chromium -- running headless with
            # nothing to report to. Measured: 3 orphaned processes and ~600 MB
            # still resident after one restart during a batch.
            runner.shutdown()

    @staticmethod
    def _wsgi():
        from testhub.wsgi import application
        return application

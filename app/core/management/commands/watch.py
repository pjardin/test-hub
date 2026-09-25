"""Follow a run live: status changes + its log, until it finishes.
Ctrl+C stops WATCHING, never the run."""
import time

from django.core.management.base import BaseCommand, CommandError

from core.models import Run
from core.services import terminal


class Command(BaseCommand):
    help = "Follow a run (default: the newest active one) until it finishes"

    def add_arguments(self, parser):
        parser.add_argument("run_id", nargs="?", type=int)

    def handle(self, *args, **opts):
        run_id = opts["run_id"]
        if not run_id:
            act = terminal.active_runs()
            if not act:
                self.stdout.write("nothing is running or queued.")
                return
            run_id = act[-1].pk
        if not Run.objects.filter(pk=run_id).exists():
            raise CommandError(f"no run {run_id}")
        self.stdout.write(f"watching run {run_id} -- Ctrl+C stops watching, never the run")
        offset = 0
        last_status = ""
        while True:
            run = Run.objects.get(pk=run_id)
            if run.status != last_status:
                self.stdout.write(f"[run {run_id}] {run.status}")
                last_status = run.status
            log_path = terminal.log_path_for(run)
            if log_path and log_path.exists():
                with open(log_path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                if chunk:
                    self.stdout.write(chunk.decode("utf-8", "replace"), ending="")
                    offset += len(chunk)
            if run.status in Run.FINISHED_STATUSES:
                dur = terminal.fmt_dur(run.duration_seconds)
                first = (run.error_message or "").splitlines()[0] if run.error_message else ""
                self.stdout.write(f"\n[run {run_id}] finished: {run.status.upper()} ({dur})"
                                  + (f" -- {first}" if first else ""))
                raise SystemExit(0 if run.status == Run.PASSED else 1)
            time.sleep(1)

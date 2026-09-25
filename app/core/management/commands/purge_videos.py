"""testhub purge_videos [--delete] -- remove every run's continuous video.

The activity replay (frames saved only while the page changes) is a run's
picture; the continuous .webm is the largest file a run keeps and repeats it.
Since 2.17 it is off by default (runner.video); this removes the ones already
stored. Without --delete it only reports what it would remove. With --delete
it removes video.webm -- from local disk and, for runs offloaded to S3, from
the bucket -- and nothing else: frames, screenshots, trace, log and every
run's history stay.
"""
from django.core.management.base import BaseCommand

from core.services import housekeeping


def _mb(n):
    return f"{n / 1_000_000:.1f} MB"


class Command(BaseCommand):
    help = "Remove every run's continuous video (reports only, unless --delete)"

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true",
                            help="actually delete (default: only report)")

    def handle(self, *args, **opts):
        report = housekeeping.delete_videos(dry_run=not opts["delete"])
        size = f"{_mb(report['local_bytes'])} on this disk"
        if report["remote_files"]:
            size += f", {_mb(report['remote_bytes'])} in S3 ({report['remote_files']} file(s))"
        if not report["runs"]:
            self.stdout.write("no run has a video -- nothing to remove")
        elif not opts["delete"]:
            self.stdout.write(f"{report['runs']} run(s) have a video: {size}. "
                              "Run again with --delete to remove them (nothing else is touched).")
        else:
            self.stdout.write(f"removed the video of {report['deleted']} run(s) ({size})")
            if report["not_deleted"]:
                self.stdout.write(f"{report['not_deleted']} could not be removed from S3 "
                                  "(see the log); their links still work")

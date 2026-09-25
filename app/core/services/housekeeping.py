"""Results housekeeping: artifacts (videos, traces, frames) are the only
thing in the hub that grows without bound. Pruning deletes OLD RUN
ARTIFACT FOLDERS ONLY -- the database rows, and therefore every chart,
pass rate, timing trend and history strip, are untouched. Run pages whose
folder is gone simply show no video/screenshots.
"""
import json
import logging
import shutil
import time
from pathlib import Path

from core import appconfig

log = logging.getLogger("testhub.housekeeping")


_usage_cache = {"at": 0.0, "value": None}
USAGE_TTL = 120     # seconds


def results_usage(max_age=USAGE_TTL) -> dict:
    """Total bytes and run-folder count under the results dir.

    Cached: this walks every artifact file, which is fine at a few hundred
    runs and ruinous at a hundred thousand (measured minutes on network
    storage) -- and it was running on every Settings page load.
    """
    import time as _time
    if (_usage_cache["value"] is not None
            and _time.time() - _usage_cache["at"] < max_age):
        return _usage_cache["value"]
    value = _results_usage_uncached()
    _usage_cache.update({"at": _time.time(), "value": value})
    return value


def _results_usage_uncached() -> dict:
    cfg = appconfig.get_config()
    total = 0
    run_dirs = 0
    root = cfg.results_dir
    if not root.is_dir():
        return {"bytes": 0, "run_dirs": 0}
    for test_dir in root.iterdir():
        if not test_dir.is_dir():
            continue
        for run_dir in test_dir.iterdir():
            if not run_dir.is_dir():
                continue
            run_dirs += 1
            for f in run_dir.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    continue
    return {"bytes": total, "run_dirs": run_dirs}


def prune_results(older_than_days: int) -> dict:
    """Delete run artifact folders whose newest file is older than the
    cutoff. Returns what was removed."""
    cfg = appconfig.get_config()
    cutoff = time.time() - older_than_days * 86400
    removed = 0
    freed = 0
    root = cfg.results_dir
    if not root.is_dir() or older_than_days <= 0:
        return {"removed": 0, "freed_bytes": 0}
    for test_dir in sorted(root.iterdir()):
        if not test_dir.is_dir():
            continue
        for run_dir in sorted(test_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            try:
                newest = max((f.stat().st_mtime for f in run_dir.rglob("*")
                              if f.is_file()), default=run_dir.stat().st_mtime)
            except OSError:
                continue
            if newest >= cutoff:
                continue
            size = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
            shutil.rmtree(run_dir, ignore_errors=True)
            removed += 1
            freed += size
        # drop now-empty per-test folders
        try:
            if test_dir.is_dir() and not any(test_dir.iterdir()):
                test_dir.rmdir()
        except OSError:
            pass
    if removed:
        _usage_cache["value"] = None
        log.info("pruned %d run folder(s), freed %.1f MB",
                 removed, freed / 1e6)
    return {"removed": removed, "freed_bytes": freed}


def startup_prune():
    """Called by `serve`: honour maintenance.retention_days if set (>0)."""
    cfg = appconfig.get_config()
    days = cfg.retention_days
    if days > 0:
        result = prune_results(days)
        if result["removed"]:
            log.info("startup retention (%dd): removed %d old run folder(s)",
                     days, result["removed"])


# ---------------------------------------------------------------------------
# deleting run data (the "I am running out of disk" tools)
# ---------------------------------------------------------------------------
#
# Two levels, deliberately distinct:
#
#   artifacts only  the videos/traces/screenshots go; the Run row stays, so
#                   pass rates, durations, timings and every chart survive.
#                   This is what reclaims ~all the space.
#   whole run       the row goes too. Only for genuinely unwanted runs --
#                   it changes history.

def delete_run_artifacts(run) -> int:
    """Delete one run's files (local and, if offloaded, in the bucket).
    Returns bytes freed locally."""
    from core.services import storage
    cfg = appconfig.get_config()
    freed = 0
    if run.artifacts_rel:
        base = cfg.results_dir / run.artifacts_rel
        if base.is_dir():
            freed = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
            shutil.rmtree(base, ignore_errors=True)
        if run.artifacts_remote:
            storage.delete_run_objects(run)
    run.artifacts_remote = False
    run.artifacts_index_json = "[]"
    run.save(update_fields=["artifacts_remote", "artifacts_index_json"])
    _usage_cache["value"] = None      # the cached total is now wrong
    return freed


def delete_run(run) -> int:
    """Artifacts AND the history row."""
    freed = delete_run_artifacts(run)
    run.delete()
    return freed


def _purge_runs(victims, artifacts_only=True) -> int:
    """Delete a batch of runs' files, then settle the database in TWO queries
    instead of two per run. Purging 10k runs used to fire 20k statements and
    hold SQLite's write lock long enough to stall the runner; this keeps the
    slow part (unlinking files) outside the transaction."""
    from core.models import Run
    from core.services import storage
    cfg = appconfig.get_config()
    freed, pks = 0, []
    for run in victims:
        pks.append(run.pk)
        if not run.artifacts_rel:
            continue
        base = cfg.results_dir / run.artifacts_rel
        if base.is_dir():
            freed += sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
            shutil.rmtree(base, ignore_errors=True)
        if run.artifacts_remote:
            storage.delete_run_objects(run)
    if pks:
        _usage_cache["value"] = None
        for chunk in (pks[i:i + 500] for i in range(0, len(pks), 500)):
            if artifacts_only:
                Run.objects.filter(pk__in=chunk).update(artifacts_remote=False,
                                                        artifacts_index_json="[]")
            else:
                Run.objects.filter(pk__in=chunk).delete()
    return freed


def purge_test(test, artifacts_only=True, keep_last=0) -> dict:
    """Clear a single test's run data. keep_last>0 protects the newest N
    runs, which is the usual 'free space but keep recent evidence'."""
    from core.models import Run
    runs = list(Run.objects.filter(test=test, status__in=Run.FINISHED_STATUSES)
                .order_by("-queued_at"))
    victims = runs[keep_last:] if keep_last else runs
    return {"runs": len(victims), "freed_bytes": _purge_runs(victims, artifacts_only),
            "artifacts_only": artifacts_only}


def purge_all(artifacts_only=True, keep_last_per_test=0, status=None) -> dict:
    """Bulk cleanup across every test. `status` restricts it (e.g. only
    'passed' runs -- failures are usually the ones worth keeping)."""
    from core.models import Run, Test
    victims = []
    if keep_last_per_test:
        # Per-test protection needs per-test ordering, so walk the tests.
        for test in Test.objects.all():
            runs = list(Run.objects.filter(test=test, status__in=Run.FINISHED_STATUSES)
                        .order_by("-queued_at"))
            victims.extend(runs[keep_last_per_test:])
    else:
        victims = list(Run.objects.filter(status__in=Run.FINISHED_STATUSES))
    if status:
        victims = [r for r in victims if r.status == status]
    return {"runs": len(victims), "freed_bytes": _purge_runs(victims, artifacts_only),
            "artifacts_only": artifacts_only}


def delete_videos(dry_run=True) -> dict:
    """Every run's continuous video (video.webm) -- on local disk and, for
    offloaded runs, in the bucket -- and NOTHING else: frames, screenshots,
    trace, log and the history row all stay. The activity replay is the
    run's picture; the video is the largest file a run keeps (2.17 stopped
    recording it by default). dry_run only counts."""
    from core.models import Run
    from core.services import storage
    cfg = appconfig.get_config()
    report = {"runs": 0, "local_bytes": 0, "remote_files": 0, "remote_bytes": 0,
              "deleted": 0, "not_deleted": 0}
    candidates = list(Run.objects.exclude(artifacts_rel="")
                      .values_list("pk", "artifacts_rel", "artifacts_remote", "artifacts_index_json"))
    for pk, rel, remote, index_json in candidates:
        local = cfg.results_dir / rel / "video.webm"
        try:
            index = json.loads(index_json or "[]") if remote else []
        except ValueError:
            index = []
        in_bucket = "video.webm" in index
        if not (local.is_file() or in_bucket):
            continue
        report["runs"] += 1
        if local.is_file():
            report["local_bytes"] += local.stat().st_size
        if in_bucket:
            report["remote_files"] += 1
            report["remote_bytes"] += storage.object_size(f"{rel}/video.webm") or 0
        if dry_run:
            continue
        ok = True
        if local.is_file():
            local.unlink()
        if in_bucket:
            if storage.delete_object(f"{rel}/video.webm"):
                index.remove("video.webm")
                Run.objects.filter(pk=pk).update(artifacts_index_json=json.dumps(index))
            else:
                ok = False        # keep the index entry: the link still works
        report["deleted" if ok else "not_deleted"] += 1
    _usage_cache["value"] = None
    return report


"""Where run artifacts live.

Default: local disk (`data/results/...`) — the only thing that works on an
air-gapped machine, and the default everywhere.

Optional: an S3 bucket. On AWS, videos/traces/frames are what actually
grow, and the instance disk is the wrong place for years of them. With
`storage.backend: "s3"` the hub uploads each finished run's artifacts,
optionally deletes the local copy, and serves them afterwards through
short-lived presigned URLs. The DATABASE always stays local (it is small,
and it is what every chart reads).

boto3 is imported lazily so an offline machine never pays for a feature it
does not use — and a missing/misconfigured bucket degrades to "keep it
local" rather than losing a run.
"""
import json
import logging
import shutil
from pathlib import Path

from core import appconfig

log = logging.getLogger("testhub.storage")

_client = None
_client_key = None


def enabled() -> bool:
    return appconfig.get_config().storage_backend == "s3"


def _cfg():
    return appconfig.get_config()


def client():
    """Cached boto3 S3 client, or None with a logged reason."""
    global _client, _client_key
    cfg = _cfg()
    key = (cfg.s3_bucket, cfg.s3_region, cfg.s3_endpoint)
    if _client is not None and _client_key == key:
        return _client
    try:
        import boto3
    except ImportError:
        log.warning("storage.backend is s3 but boto3 is not installed "
                    "(re-run install-app.sh from an app zip that bundles it)")
        return None
    kwargs = {}
    if cfg.s3_region:
        kwargs["region_name"] = cfg.s3_region
    if cfg.s3_endpoint:
        kwargs["endpoint_url"] = cfg.s3_endpoint
    # Credentials come from the instance role / environment / ~/.aws --
    # never from config.json, which is world-readable on some boxes.
    _client = boto3.client("s3", **kwargs)
    _client_key = key
    return _client


def check() -> dict:
    """Settings-page 'test connection': can we really read and write?"""
    cfg = _cfg()
    if not enabled():
        return {"ok": False, "error": "storage.backend is not 's3'"}
    if not cfg.s3_bucket:
        return {"ok": False, "error": "storage.bucket is empty"}
    s3 = client()
    if s3 is None:
        return {"ok": False, "error": "boto3 is not available in this install"}
    key = f"{cfg.s3_prefix}/.testhub-write-check".lstrip("/")
    try:
        s3.put_object(Bucket=cfg.s3_bucket, Key=key, Body=b"ok")
        s3.get_object(Bucket=cfg.s3_bucket, Key=key)
        s3.delete_object(Bucket=cfg.s3_bucket, Key=key)
        return {"ok": True, "detail": f"read/write confirmed on "
                                      f"s3://{cfg.s3_bucket}/{cfg.s3_prefix}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _key_for(rel_path: str) -> str:
    prefix = _cfg().s3_prefix.strip("/")
    return f"{prefix}/{rel_path}".lstrip("/")


def upload_run(run) -> dict:
    """Push one finished run's artifacts. Returns a report; never raises --
    a storage problem must not lose the run or crash the runner."""
    cfg = _cfg()
    if not enabled() or not run.artifacts_rel:
        return {"uploaded": 0, "skipped": True}
    base = cfg.results_dir / run.artifacts_rel
    if not base.is_dir():
        return {"uploaded": 0, "skipped": True}
    s3 = client()
    if s3 is None:
        return {"uploaded": 0, "error": "boto3 unavailable"}

    files, uploaded, total_bytes = [], 0, 0
    try:
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = f"{run.artifacts_rel}/{path.relative_to(base)}"
            s3.upload_file(str(path), cfg.s3_bucket, _key_for(rel))
            files.append(str(path.relative_to(base)))
            uploaded += 1
            total_bytes += path.stat().st_size
    except Exception as exc:
        log.warning("run %s: S3 upload failed (%s) — artifacts stay local",
                    run.pk, exc)
        return {"uploaded": uploaded, "error": str(exc)[:200]}

    # The index is what the UI reads AND what delete_run_objects() walks, so
    # truncating it would both hide artifacts and orphan (billable) objects.
    # A busy run can hold ~1500 frames plus screenshots, so keep the lot.
    run.artifacts_index_json = json.dumps(files)
    run.artifacts_remote = True
    run.save(update_fields=["artifacts_index_json", "artifacts_remote"])

    if cfg.s3_delete_local:
        shutil.rmtree(base, ignore_errors=True)
    log.info("run %s: uploaded %d artifact file(s), %.1f MB%s",
             run.pk, uploaded, total_bytes / 1e6,
             " (local copy removed)" if cfg.s3_delete_local else "")
    return {"uploaded": uploaded, "bytes": total_bytes,
            "local_removed": cfg.s3_delete_local}


def read_tail(rel_path: str, max_bytes: int = 20000) -> "str | None":
    """The last `max_bytes` of an offloaded text file -- one ranged GET, so
    the run page can show an offloaded run's log like a local one. None
    when the bucket cannot be read (the page then links to the file)."""
    cfg = _cfg()
    s3 = client()
    if s3 is None:
        return None
    try:
        obj = s3.get_object(Bucket=cfg.s3_bucket, Key=_key_for(rel_path),
                            Range=f"bytes=-{int(max_bytes)}")
        return obj["Body"].read().decode("utf-8", "replace")
    except Exception as exc:
        log.warning("could not read %s from the bucket: %s", rel_path, exc)
        return None


def object_size(rel_path: str) -> "int | None":
    """Bytes of one offloaded file (a HEAD request), or None."""
    cfg = _cfg()
    s3 = client()
    if s3 is None:
        return None
    try:
        return int(s3.head_object(Bucket=cfg.s3_bucket, Key=_key_for(rel_path))["ContentLength"])
    except Exception:
        return None


def delete_object(rel_path: str) -> bool:
    """Delete one offloaded file. True when the bucket confirmed it (deleting
    a key that is already gone also counts: S3 deletes are idempotent)."""
    cfg = _cfg()
    s3 = client()
    if s3 is None:
        return False
    try:
        s3.delete_object(Bucket=cfg.s3_bucket, Key=_key_for(rel_path))
        return True
    except Exception as exc:
        log.warning("could not delete %s from the bucket: %s", rel_path, exc)
        return False


def presigned_url(rel_path: str) -> "str | None":
    cfg = _cfg()
    s3 = client()
    if s3 is None:
        return None
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": cfg.s3_bucket, "Key": _key_for(rel_path)},
            ExpiresIn=cfg.s3_url_expiry)
    except Exception as exc:
        log.warning("presign failed for %s: %s", rel_path, exc)
        return None


def delete_run_objects(run) -> int:
    """Remove a run's objects from the bucket (used by the delete tools)."""
    cfg = _cfg()
    if not run.artifacts_remote or not run.artifacts_rel:
        return 0
    s3 = client()
    if s3 is None:
        return 0
    try:
        files = json.loads(run.artifacts_index_json or "[]")
    except ValueError:
        files = []
    deleted = 0
    for rel in files:
        try:
            s3.delete_object(Bucket=cfg.s3_bucket,
                             Key=_key_for(f"{run.artifacts_rel}/{rel}"))
            deleted += 1
        except Exception:
            continue
    return deleted


def offload_backlog(limit=500) -> dict:
    """Upload artifacts of runs that finished before S3 was switched on."""
    from core.models import Run
    if not enabled():
        return {"uploaded_runs": 0, "error": "storage.backend is not 's3'"}
    cfg = _cfg()
    done = failed = 0
    for run in (Run.objects.filter(artifacts_remote=False,
                                   status__in=Run.FINISHED_STATUSES)
                .exclude(artifacts_rel="").order_by("pk")[:limit]):
        if not (cfg.results_dir / run.artifacts_rel).is_dir():
            continue
        result = upload_run(run)
        if result.get("error"):
            failed += 1
        elif result.get("uploaded"):
            done += 1
    return {"uploaded_runs": done, "failed_runs": failed}

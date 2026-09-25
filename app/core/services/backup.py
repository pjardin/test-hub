"""Backup, restore and test transfer.

Two different jobs, deliberately separate:

  BACKUP   everything that would hurt to lose on THIS machine — the tests
           folder, the database (run history) and config.json. Not the
           artifacts: they are large, replaceable and already prunable.

  TESTS ZIP  just the test definitions (.py + .json + _groups.json). This
           is the disk-transfer format: export on the laptop, import at
           work. It never carries run history, so importing cannot damage
           the target machine's records.

Both are plain zips; both can be produced and consumed from the UI or the
CLI, because on a hard-to-reach machine the CLI is sometimes all there is.
"""
import io
import json
import os
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

from core import appconfig

TESTS_MANIFEST = "_export.json"


# ---------------------------------------------------------------------------
# full backup
# ---------------------------------------------------------------------------

def _safe_db_copy(db_path: Path, dest: Path) -> bool:
    """Copy SQLite consistently even while the hub is running (the backup
    API takes care of pages being written under us)."""
    if not db_path.exists():
        return False
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(dest))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return True
    except sqlite3.Error:
        shutil.copy2(db_path, dest)   # last resort
        return True


def build_backup(include_results=False) -> bytes:
    """The whole restorable state as a zip, in memory (small: the DB plus
    text files; artifacts excluded unless asked for)."""
    cfg = appconfig.get_config()
    buf = io.BytesIO()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("BACKUP.json", json.dumps({
            "created": stamp,
            "app_version": (appconfig.APP_DIR / "VERSION").read_text().strip()
            if (appconfig.APP_DIR / "VERSION").exists() else "unknown",
            "includes_results": include_results,
            "note": "restore with: testhub restore <this file>",
        }, indent=2))

        for path in sorted(cfg.tests_dir.rglob("*")):
            if path.is_file():
                zf.write(path, f"tests/{path.relative_to(cfg.tests_dir)}")

        if cfg.path.exists():
            zf.write(cfg.path, "config.json")

        # secrets.json is deliberately NOT in the backup. A backup is carried
        # on a disc and restored on other machines; credentials are supposed
        # to stay on the one machine that needs them. Losing them to a rebuild
        # costs a re-entry in Settings, which is the cheaper mistake.

        db = cfg.data_dir / "db.sqlite3"
        tmp = cfg.data_dir / f".backup-{stamp}.sqlite3"
        if _safe_db_copy(db, tmp):
            zf.write(tmp, "db.sqlite3")
            tmp.unlink(missing_ok=True)

        if include_results:
            for path in sorted(cfg.results_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, f"results/{path.relative_to(cfg.results_dir)}")
    return buf.getvalue()


def restore_backup(zip_path: Path, restore_db=True) -> dict:
    """Put a backup back. The current tests folder and database are moved
    aside first (never deleted) so a wrong restore is undoable."""
    cfg = appconfig.get_config()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = {"tests": 0, "db": False, "config": False, "kept_at": None}

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if "BACKUP.json" not in names:
            raise ValueError("not a Test Hub backup (no BACKUP.json inside)")

        aside = cfg.data_dir / f"_replaced-{stamp}"
        aside.mkdir(parents=True, exist_ok=True)
        if cfg.tests_dir.exists():
            shutil.move(str(cfg.tests_dir), str(aside / "tests"))
        cfg.tests_dir.mkdir(parents=True, exist_ok=True)
        report["kept_at"] = str(aside)

        tests_root = cfg.tests_dir.resolve()
        for name in names:
            if name.startswith("tests/") and not name.endswith("/"):
                # Containment check: a backup zip is carried between machines
                # on a disc, so treat it as untrusted input. Without this,
                # an entry like tests/../../../PWNED writes outside the data
                # directory (classic zip-slip).
                target = (cfg.tests_dir / name[len("tests/"):]).resolve()
                if tests_root != target.parent and tests_root not in target.parents:
                    report.setdefault("rejected", []).append(name)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))
                report["tests"] += 1

        if restore_db and "db.sqlite3" in names:
            db = cfg.data_dir / "db.sqlite3"
            if db.exists():
                shutil.move(str(db), str(aside / "db.sqlite3"))
            db.write_bytes(zf.read("db.sqlite3"))
            report["db"] = True

    return report


def auto_backup(reason="upgrade") -> Path:
    """A retained on-disk backup (kept: last 10). Called before risky
    operations like a restore or a schema migration.

    Created 0600 from the first byte (os.open with the mode, never
    write-then-chmod): the zip contains db.sqlite3, whose django_session
    rows are login cookies — a world-readable backup would undo the 0600
    on the database itself."""
    cfg = appconfig.get_config()
    folder = cfg.data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"backup-{reason}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(build_backup())
    existing = sorted(folder.glob("backup-*.zip"))
    for old in existing[:-10]:
        old.unlink(missing_ok=True)
    return path


def backup_before_migrating() -> "Path | None":
    """A safety backup when -- and only when -- a migration is pending on a
    database that already holds data. install-app.sh copies the database
    before it migrates; the everything-image keeps its LIVE database in a
    volume that installer never sees, and `serve` migrates it at start
    (2.16.0 migrated pwlab's with no copy taken). So `serve` calls this
    first. Reads files only: the schema is still the old one here."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connection)
    if not executor.loader.applied_migrations:       # a fresh install: nothing to lose
        return None
    if not executor.migration_plan(executor.loader.graph.leaf_nodes()):
        return None
    return auto_backup("pre-migrate")


# ---------------------------------------------------------------------------
# tests-only transfer (the disk workflow)
# ---------------------------------------------------------------------------

def export_tests(test_ids=None) -> bytes:
    """Test definitions as a zip: what you carry between machines."""
    cfg = appconfig.get_config()
    buf = io.BytesIO()
    wanted = set(test_ids) if test_ids else None
    exported = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        from core.services.syncer import sidecar_path as _sidecar_path
        from core.services.syncer import test_stem as _test_stem
        test_files = sorted(
            list(cfg.tests_dir.glob("*.py")) + list(cfg.tests_dir.glob("*.spec.ts")),
            key=lambda p: p.name,
        )
        for path in test_files:
            if path.name.startswith("_"):
                continue
            test_id = _test_stem(path).split("__", 1)[0]
            if wanted and test_id not in wanted:
                continue
            exported.append(test_id)
            zf.write(path, path.name)
            sidecar = _sidecar_path(path)
            if sidecar.exists():
                zf.write(sidecar, sidecar.name)
        groups = cfg.tests_dir / "_groups.json"
        if groups.exists():
            zf.write(groups, groups.name)
        # Shared code the tests import (page objects, helpers — see
        # CONNECT-YOUR-TESTS.md): top-level _*.py files and everything under
        # _-prefixed packages such as _lib/. The syncer ignores these names,
        # the harness makes them importable, and they must travel WITH the
        # tests or an exported suite arrives broken. __pycache__ never ships.
        for path in sorted(cfg.tests_dir.glob("_*")):
            if path.name in (groups.name, "__pycache__"):
                continue
            if path.is_file() and path.suffix in (".py", ".json", ".ts"):
                zf.write(path, path.name)
            elif path.is_dir():
                for sub in sorted(path.rglob("*")):
                    if "__pycache__" in sub.parts or sub.name.startswith("."):
                        continue
                    if sub.is_file() and sub.suffix in (".py", ".json", ".ts"):
                        zf.write(sub, str(sub.relative_to(cfg.tests_dir)))
        zf.writestr(TESTS_MANIFEST, json.dumps({
            "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tests": exported,
            "note": "import from the Settings page or: testhub import <file>",
        }, indent=2))
    return buf.getvalue()


def import_tests(zip_path, overwrite=True) -> dict:
    """Bring test definitions in from a zip. Only ever touches the tests
    folder — never run history. Tests stay FLAT (a path on one is refused);
    the one shape allowed a path is shared code under a _-prefixed package
    (_lib/pages.py), because that is where exported suites carry their page
    objects. Everything else — traversal, hidden files, other extensions —
    is rejected, so a wrong file cannot scribble over the machine."""
    cfg = appconfig.get_config()
    report = {"written": [], "skipped": [], "errors": []}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("/") or name == TESTS_MANIFEST:
                continue
            rel = Path(name)
            parts = rel.parts
            bad = (rel.is_absolute() or ".." in parts
                   or any(p.startswith(".") for p in parts)
                   or "__pycache__" in parts
                   # a path is only legitimate under a _-prefixed package
                   or (len(parts) > 1 and not parts[0].startswith("_")))
            if bad:
                report["errors"].append(f"{name}: unexpected path, ignored")
                continue
            if rel.suffix not in (".py", ".json", ".ts"):
                report["errors"].append(f"{rel.name}: not a test file, ignored")
                continue
            target = cfg.tests_dir / rel
            # Containment despite the checks above: these zips arrive on
            # discs and are untrusted by design.
            try:
                target.resolve().relative_to(cfg.tests_dir.resolve())
            except ValueError:
                report["errors"].append(f"{name}: escapes the tests folder, ignored")
                continue
            if target.exists() and not overwrite:
                report["skipped"].append(str(rel))
                continue
            data = zf.read(name)
            if rel.suffix == ".json":
                try:
                    json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    report["errors"].append(f"{rel.name}: invalid JSON ({exc})")
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            report["written"].append(str(rel))
    return report

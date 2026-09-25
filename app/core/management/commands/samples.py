"""testhub samples [--add-new] [--update] -- bring newer bundled sample tests
into this hub.

A fresh install copies every bundled sample test into the tests folder. An
UPGRADE never touches that folder -- the tests in it are yours -- so sample
tests that a newer release ships (2.13's Acme Freight tests, for instance)
would otherwise never appear on a hub that was installed earlier. This lists
what the app bundles that this hub lacks; --add-new copies exactly that:

  * a sample test is copied only if NO test with its id exists here, nor in
    tests/_archive/ -- nothing is ever overwritten or resurrected;
  * shared-code files under _lib/ are copied only if missing;
  * sample GROUPS are added to _groups.json only when no group of that name
    exists -- your own groups stay exactly as they are.

A sample's CODE you already have (a test's .py/.spec.ts, a _lib/ file) that
differs from this version's copy is listed with a star and left alone: it
may be an older sample, or it may hold your own edits -- a byte comparison
cannot tell which. --update takes this version's copy and keeps yours beside
it as <name>.before-<version> (never deleted). Sidecars (schedules, settings)
and groups are never replaced: those are the lab's, whatever the sample says.

Then the tests folder is re-synced, so the new tests appear at once (the
serving hub reads them from the same database; no restart).
"""
import json
import os
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import appconfig
from core.services.syncer import (GROUPS_FILE, TS_SUFFIX, _atomic_write,
                                  id_from_stem, sidecar_path, sync_from_files,
                                  test_stem)

SAMPLES = appconfig.APP_DIR / "sample_tests"


def _test_files(folder):
    if not folder.is_dir():
        return []
    return sorted(p for p in list(folder.glob("*.py")) + list(folder.glob("*" + TS_SUFFIX))
                  if not p.name.startswith("_"))


def _differs(here, src):
    return here.is_file() and here.read_bytes() != src.read_bytes()


def plan(tests_dir):
    """What --add-new would copy: (tests, lib_files, groups), plus the code
    files present here that differ from this version's copy (changed, paths
    relative to the tests folder), which only --update replaces."""
    have = {id_from_stem(test_stem(p))
            for p in _test_files(tests_dir) + _test_files(tests_dir / "_archive")}
    tests, changed = [], []
    for p in _test_files(SAMPLES):
        if id_from_stem(test_stem(p)) not in have:
            tests.append(p)
        elif _differs(tests_dir / p.name, p):
            changed.append(Path(p.name))

    lib_files = []
    lib = SAMPLES / "_lib"
    if lib.is_dir():
        for src in sorted(lib.rglob("*")):
            if src.is_file() and "__pycache__" not in src.parts:
                rel = src.relative_to(SAMPLES)
                if not (tests_dir / rel).exists():
                    lib_files.append(rel)
                elif _differs(tests_dir / rel, src):
                    changed.append(rel)

    groups = []
    sample_groups = _read_groups(SAMPLES / GROUPS_FILE)
    current = {g.get("name") for g in _read_groups(tests_dir / GROUPS_FILE)}
    for group in sample_groups:
        if group.get("name") and group["name"] not in current:
            groups.append(group)
    return tests, lib_files, groups, changed


def _keep_name(path, version):
    """<name>.before-<version>, numbered if that name is already taken. The
    suffix also keeps it out of the syncer's *.py / *.spec.ts globs."""
    keep = path.with_name(f"{path.name}.before-{version}")
    n = 1
    while keep.exists():
        keep = path.with_name(f"{path.name}.before-{version}.{n}")
        n += 1
    return keep


def _replace_keeping_yours(here, src, version):
    """Yours is copied aside FIRST, then the new file swaps in atomically --
    at no moment is the file missing or half-written."""
    keep = _keep_name(here, version)
    shutil.copy2(here, keep)
    tmp = here.with_name(f".{here.name}.new")
    shutil.copyfile(src, tmp)           # fresh mtime: a stale .pyc cannot win
    os.replace(tmp, here)
    return keep


def _read_groups(path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CommandError(f"{path} is not valid JSON ({exc}); fix it first -- "
                           "this command never overwrites a file it cannot read")
    groups = data.get("groups", []) if isinstance(data, dict) else []
    return [g for g in groups if isinstance(g, dict)]


class Command(BaseCommand):
    help = ("List bundled sample tests this hub does not have yet; "
            "--add-new copies them (never overwrites anything)")

    def add_arguments(self, parser):
        parser.add_argument("--add-new", action="store_true",
                            help="copy the missing sample tests, _lib files and groups")
        parser.add_argument("--update", action="store_true",
                            help="replace sample code you have that differs from this "
                                 "version's copy (yours is kept beside it)")

    def handle(self, *args, **opts):
        tests_dir = appconfig.get_config().tests_dir
        tests, lib_files, groups, changed = plan(tests_dir)
        if not (tests or lib_files or groups):
            self.stdout.write("every bundled sample test is already here -- nothing to add")
            if not changed:
                return
        for p in tests:
            self.stdout.write(f"  test   {id_from_stem(test_stem(p)):10s} {p.name}")
        for rel in lib_files:
            self.stdout.write(f"  lib    {rel}")
        for g in groups:
            self.stdout.write(f"  group  {g['name']}  ({len(g.get('tests', []))} tests)")
        for rel in changed:
            kind = "lib*" if rel.parts[0] == "_lib" else "test*"
            self.stdout.write(f"  {kind:6s} {rel}  -- yours differs from this version's copy")
        if not (opts["add_new"] or opts["update"]):
            if tests or lib_files or groups:
                self.stdout.write("run again with --add-new to copy what is new "
                                  "(nothing existing is touched)")
            if changed:
                self.stdout.write("starred files are kept as they are; --update takes this "
                                  "version's copy and keeps yours beside it as "
                                  "<name>.before-<version>")
            return

        if opts["update"] and changed:
            version = (appconfig.APP_DIR / "VERSION").read_text().strip() or "update"
            for rel in changed:
                keep = _replace_keeping_yours(tests_dir / rel, SAMPLES / rel, version)
                self.stdout.write(f"updated {rel} (yours is kept as {keep.name})")
        if opts["add_new"]:
            self._add(tests_dir, tests, lib_files, groups)
        result = sync_from_files()
        self.stdout.write(f"rescanned: {result}")

    def _add(self, tests_dir, tests, lib_files, groups):
        tests_dir.mkdir(parents=True, exist_ok=True)
        for p in tests:
            shutil.copy2(p, tests_dir / p.name)
            sidecar = sidecar_path(p)
            if sidecar.exists() and not (tests_dir / sidecar.name).exists():
                shutil.copy2(sidecar, tests_dir / sidecar.name)
        for rel in lib_files:
            (tests_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SAMPLES / rel, tests_dir / rel)
        if groups:
            target = tests_dir / GROUPS_FILE
            data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
            if not isinstance(data, dict):
                data = {}
            data["groups"] = _read_groups(target) + groups
            _atomic_write(target, json.dumps(data, indent=2) + "\n")
        self.stdout.write(f"added {len(tests)} test(s), {len(lib_files)} shared file(s), "
                          f"{len(groups)} group(s)")

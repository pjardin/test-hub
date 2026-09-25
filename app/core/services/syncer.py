"""Files <-> DB synchronisation.

The tests/ folder is the source of truth for what tests EXIST and how they
are tagged, grouped and scheduled:

    tests/
      LOGIN-001__login_smoke.py      the Playwright test (def run(page, ctx))
      LOGIN-001__login_smoke.json    its metadata sidecar
      _groups.json                   group definitions (+ group schedules)

The test id is everything before the first '__' in the filename (or the whole
stem if there is no '__'). That id is the stable key: rename the rest of the
file, move machines, re-sync -- history follows the id.

sync_from_files() is called at server start, from `manage.py sync`, and from
the Rescan button. UI edits go the other way: the DB row is updated first and
the sidecar / _groups.json is regenerated, so a later sync is a no-op and the
files can be copied to another machine with nothing lost.
"""
import json
import logging
import os
import re
from datetime import time as dtime
from pathlib import Path

from django.db import transaction

from core import appconfig
from core.models import DAY_NAMES, Group, Schedule, Test

log = logging.getLogger("testhub.sync")

GROUPS_FILE = "_groups.json"

NEW_TEST_TEMPLATE = '''"""{name}

Runs against ctx.base_url (the target URL from config.json).
Raise AssertionError (or any exception) to fail the test.
"""


def run(page, ctx):
    page.goto(ctx.base_url)
    ctx.log(f"loaded {{page.url}}")
    ctx.screenshot("loaded")

    assert page.title(), "page has no title"
'''

NEW_TS_TEST_TEMPLATE = '''import {{ test, expect }} from '@playwright/test';

// {name}
// Runs under the TS engine's @playwright/test; baseURL is the hub's
// target.url, so relative page.goto() paths work.
test('{name}', async ({{ page }}) => {{
  await page.goto('./');   // relative to target.url (works when it has a path)
  await expect(page).toHaveTitle(/./);
}});
'''


# ---------------------------------------------------------------------------
# filename / sidecar helpers
# ---------------------------------------------------------------------------

def id_from_stem(stem: str) -> str:
    return stem.split("__", 1)[0].strip()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return slug or "test"


def validate_test_id(test_id: str) -> str:
    """One source of truth for what a test id may be. The id becomes part
    of a FILENAME, so this is also the containment check -- everything
    that creates files from an id must go through here (create_test for
    the web, terminal.scaffold for `testhub new`)."""
    test_id = str(test_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", test_id):
        raise TestFileError(
            "Test id may contain letters, digits, '.', '-' and '_' only")
    if "__" in test_id:
        raise TestFileError("Test id must not contain a double underscore")
    return test_id


def ts_string(text: str) -> str:
    """A value made safe for a single-quoted TypeScript string literal.
    Names really do contain apostrophes ("Bob's flow") -- unescaped they
    make the generated spec unparseable."""
    return (str(text).replace("\\", "\\\\").replace("'", "\\'")
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


def docstring_safe(text: str) -> str:
    """A value made safe inside a python triple-quoted docstring: a run of
    three quotes would close it, and a trailing quote merges with the
    closing fence."""
    s = str(text).replace("\\", "\\\\").replace('"""', "'''")
    if s.endswith('"'):
        s = s[:-1] + '\\"'
    return s


def make_filename(test_id: str, name: str) -> str:
    return f"{test_id}__{slugify(name or test_id)}.py"


def make_filename_ts(test_id: str, name: str) -> str:
    return f"{test_id}__{slugify(name or test_id)}.spec.ts"


TS_SUFFIX = ".spec.ts"


def test_stem(path: Path) -> str:
    """The filename minus its test suffix. Path.stem strips only '.ts' from
    a .spec.ts file, so the '.spec' would leak into ids and sidecar names."""
    if path.name.endswith(TS_SUFFIX):
        return path.name[: -len(TS_SUFFIX)]
    return path.stem


def sidecar_path(py_path: Path) -> Path:
    # X__y.py -> X__y.json  and  X__y.spec.ts -> X__y.json (NOT .spec.json),
    # matching the sidecar convention TypeScript test repos already use.
    return py_path.parent / (test_stem(py_path) + ".json")


def days_to_names(days):
    return [DAY_NAMES[d] for d in days if 0 <= int(d) <= 6]


def names_to_days(names):
    out = []
    for n in names or []:
        if isinstance(n, int):
            if 0 <= n <= 6:
                out.append(n)
            continue
        key = str(n).strip().lower()[:3]
        if key in DAY_NAMES:
            out.append(DAY_NAMES.index(key))
    return sorted(set(out))


def parse_hhmm(value, fallback=dtime(9, 0)):
    try:
        hh, mm = str(value).strip().split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return fallback


def _atomic_write(path: Path, content: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# sidecar (de)serialisation
# ---------------------------------------------------------------------------

def sidecar_dict(test: Test) -> dict:
    return {
        "id": test.test_id,
        "name": test.name,
        "description": test.description,
        "tags": test.tags,
        "version": test.version_tag,
        "timeout_seconds": test.timeout_seconds,
        "enabled": test.enabled,
        # Omitted when unset so existing sidecars round-trip unchanged.
        **({"video": test.video_mode} if test.video_mode else {}),
        # Load plans are DEFINITIONS, so they live in the file like everything
        # else that defines a test -- otherwise a plan designed on the laptop
        # would not survive the trip to the lab on a disc.
        **({"load_plans": [
            {"name": lp.name, "description": lp.description,
             "duration_seconds": lp.duration_seconds,
             "concurrency": lp.concurrency,
             "ramp_seconds": lp.ramp_seconds,
             "think_time_seconds": lp.think_time_seconds,
             "max_iterations": lp.max_iterations,
             "keep_artifacts": lp.keep_artifacts}
            for lp in test.load_plans.all().order_by("name")]}
           if test.pk and test.load_plans.exists() else {}),
        "schedules": [
            {
                "days": days_to_names(s.days),
                "time": s.time_of_day.strftime("%H:%M"),
                "enabled": s.enabled,
                **({"browser": s.browser} if s.browser else {}),
            }
            for s in test.schedules.all()
        ],
    }


def write_sidecar(test: Test):
    cfg = appconfig.get_config()
    py_path = cfg.tests_dir / test.file_path
    _atomic_write(sidecar_path(py_path),
                  json.dumps(sidecar_dict(test), indent=2) + "\n")


def write_test_code(test: Test, code: str):
    cfg = appconfig.get_config()
    _atomic_write(cfg.tests_dir / test.file_path, code)


def read_test_code(test: Test) -> str:
    cfg = appconfig.get_config()
    path = cfg.tests_dir / test.file_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def groups_dict() -> dict:
    return {
        "groups": [
            {
                "name": g.name,
                "description": g.description,
                "tests": [t.test_id for t in g.tests.order_by("test_id")],
                "schedules": [
                    {
                        "days": days_to_names(s.days),
                        "time": s.time_of_day.strftime("%H:%M"),
                        "enabled": s.enabled,
                        **({"browser": s.browser} if s.browser else {}),
                    }
                    for s in g.schedules.all()
                ],
            }
            for g in Group.objects.prefetch_related("tests", "schedules")
        ]
    }


def write_groups_file():
    cfg = appconfig.get_config()
    _atomic_write(cfg.tests_dir / GROUPS_FILE,
                  json.dumps(groups_dict(), indent=2) + "\n")


# ---------------------------------------------------------------------------
# schedule reconciliation (shared by tests and groups)
# ---------------------------------------------------------------------------

def _apply_load_plans(test, entries):
    """Replace this test's load plans with what the sidecar says.

    Matched on name, so editing a plan's numbers in the file updates it in
    place and its past load runs stay attached.
    """
    from core.models import LoadPlan
    existing = {lp.name: lp for lp in LoadPlan.objects.filter(test=test)}
    keep = set()
    for entry in entries or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        plan = existing.get(name) or LoadPlan(test=test, name=name)
        plan.description = str(entry.get("description") or "")
        plan.duration_seconds = max(1, int(entry.get("duration_seconds") or 600))
        plan.concurrency = max(1, int(entry.get("concurrency") or 1))
        plan.ramp_seconds = max(0, int(entry.get("ramp_seconds") or 0))
        plan.think_time_seconds = max(0.0, float(entry.get("think_time_seconds") or 0))
        plan.max_iterations = max(0, int(entry.get("max_iterations") or 0))
        plan.keep_artifacts = bool(entry.get("keep_artifacts"))
        plan.save()
        keep.add(name)
    for name, plan in existing.items():
        if name not in keep:
            plan.delete()


def _apply_schedules(owner_filter: dict, entries):
    """Replace the schedules matching owner_filter with `entries`, preserving
    last_fired for entries that survive (matched on days+time)."""
    existing = {}
    for s in Schedule.objects.filter(**owner_filter):
        existing[(s.days_json, s.time_of_day.strftime("%H:%M"))] = s

    keep = set()
    for entry in entries or []:
        days = names_to_days(entry.get("days"))
        tod = parse_hhmm(entry.get("time"))
        sched = Schedule(**owner_filter)
        sched.days = days
        key = (sched.days_json, tod.strftime("%H:%M"))
        if key in existing:
            sched = existing[key]
            keep.add(key)
        sched.days = days
        sched.time_of_day = tod
        sched.enabled = bool(entry.get("enabled", True))
        sched.browser = str(entry.get("browser") or "")
        sched.save()
        keep.add(key)

    for key, sched in existing.items():
        if key not in keep:
            sched.delete()


# ---------------------------------------------------------------------------
# the sync itself
# ---------------------------------------------------------------------------

@transaction.atomic
def sync_from_files() -> dict:
    cfg = appconfig.get_config()
    cfg.ensure_dirs()
    tests_dir = cfg.tests_dir

    summary = {"created": [], "updated": [], "archived": [], "restored": [],
               "sidecars_created": [], "groups": 0, "errors": []}

    seen_ids = set()
    # Two test languages, one directory, one convention: ID__name.py runs
    # under the Python harness, ID__name.spec.ts under the TS engine's
    # @playwright/test. Everything else here (sidecars, schedules, groups,
    # history) is shared.
    test_paths = sorted(
        list(tests_dir.glob("*.py")) + list(tests_dir.glob("*" + TS_SUFFIX)),
        key=lambda p: p.name,
    )
    for py_path in test_paths:
        if py_path.name.startswith("_"):
            continue
        stem = test_stem(py_path)
        test_id = id_from_stem(stem)
        if not test_id:
            summary["errors"].append(f"{py_path.name}: cannot derive a test id")
            continue
        if test_id in seen_ids:
            summary["errors"].append(
                f"{py_path.name}: duplicate test id {test_id}, file ignored")
            continue
        seen_ids.add(test_id)

        meta = {}
        sc_path = sidecar_path(py_path)
        if sc_path.exists():
            try:
                meta = json.loads(sc_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                summary["errors"].append(f"{sc_path.name}: bad JSON ({exc}); "
                                         "using defaults")
                meta = {}
        if meta.get("id") and meta["id"] != test_id:
            summary["errors"].append(
                f"{sc_path.name}: id '{meta['id']}' does not match filename id "
                f"'{test_id}' -- the filename wins")

        test, created = Test.objects.get_or_create(
            test_id=test_id,
            defaults={"file_path": py_path.name},
        )
        test.file_path = py_path.name
        test.test_type = (Test.TYPE_TS if py_path.name.endswith(TS_SUFFIX)
                          else Test.TYPE_PYTHON)
        test.name = str(meta.get("name") or stem.split("__", 1)[-1].replace("_", " "))
        test.description = str(meta.get("description") or "")
        test.tags = meta.get("tags") or []
        test.version_tag = str(meta.get("version") or "")
        try:
            test.timeout_seconds = max(0, int(meta.get("timeout_seconds") or 0))
        except (TypeError, ValueError):
            test.timeout_seconds = 0
        test.enabled = bool(meta.get("enabled", True))
        mode = str(meta.get("video") or "").strip().lower()
        test.video_mode = mode if mode in ("on", "off") else ""
        if test.archived:
            test.archived = False
            summary["restored"].append(test_id)
        test.save()

        _apply_schedules({"test": test, "group": None}, meta.get("schedules"))
        _apply_load_plans(test, meta.get("load_plans"))

        if not sc_path.exists():
            write_sidecar(test)
            summary["sidecars_created"].append(sc_path.name)

        summary["created" if created else "updated"].append(test_id)

    # Files gone -> archive (keep history), never delete.
    for test in Test.objects.filter(archived=False).exclude(test_id__in=seen_ids):
        test.archived = True
        test.enabled = False
        test.save()
        summary["archived"].append(test.test_id)

    # ---- groups -------------------------------------------------------------
    groups_path = tests_dir / GROUPS_FILE
    if groups_path.exists():
        try:
            data = json.loads(groups_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            summary["errors"].append(f"{GROUPS_FILE}: bad JSON ({exc}); groups untouched")
            data = None
        if isinstance(data, dict):
            wanted = {}
            for gd in data.get("groups") or []:
                name = str(gd.get("name") or "").strip()
                if not name:
                    continue
                wanted[name] = gd
            for name, gd in wanted.items():
                group, _ = Group.objects.get_or_create(name=name)
                group.description = str(gd.get("description") or "")
                group.save()
                members = Test.objects.filter(test_id__in=gd.get("tests") or [])
                missing = set(gd.get("tests") or []) - {t.test_id for t in members}
                if missing:
                    summary["errors"].append(
                        f"group '{name}' references unknown tests: {sorted(missing)}")
                group.tests.set(members)
                _apply_schedules({"group": group, "test": None}, gd.get("schedules"))
            Group.objects.exclude(name__in=wanted.keys()).delete()
            summary["groups"] = len(wanted)
    else:
        summary["groups"] = Group.objects.count()

    log.info("sync: %d created, %d updated, %d archived, %d groups, %d errors",
             len(summary["created"]), len(summary["updated"]),
             len(summary["archived"]), summary["groups"], len(summary["errors"]))
    return summary


# ---------------------------------------------------------------------------
# UI-driven creation (DB row + both files in one step)
# ---------------------------------------------------------------------------

class TestFileError(Exception):
    pass


def create_test(test_id: str, name: str, code: str = "", **meta) -> Test:
    cfg = appconfig.get_config()
    test_id = validate_test_id(test_id)
    if Test.objects.filter(test_id=test_id, archived=False).exists():
        raise TestFileError(f"A test with id {test_id} already exists")

    language = str(meta.get("language") or "").strip().lower()
    if language in ("ts", "typescript"):
        filename = make_filename_ts(test_id, name)
    else:
        filename = make_filename(test_id, name)
    py_path = cfg.tests_dir / filename
    if py_path.exists():
        raise TestFileError(f"{filename} already exists in the tests folder")

    test = Test.objects.filter(test_id=test_id).first() or Test(test_id=test_id)
    test.archived = False
    test.file_path = filename
    test.test_type = (Test.TYPE_TS if filename.endswith(TS_SUFFIX)
                      else Test.TYPE_PYTHON)
    test.name = name or test_id
    test.description = meta.get("description", "")
    test.tags = meta.get("tags", [])
    test.version_tag = meta.get("version_tag", "")
    test.timeout_seconds = int(meta.get("timeout_seconds") or 0)
    mode = str(meta.get("video_mode") or "").strip().lower()
    test.video_mode = mode if mode in ("on", "off") else ""
    test.enabled = bool(meta.get("enabled", True))
    test.save()

    if not code:
        if test.test_type == Test.TYPE_TS:
            code = NEW_TS_TEST_TEMPLATE.format(name=ts_string(test.name))
        else:
            code = NEW_TEST_TEMPLATE.format(name=docstring_safe(test.name))
    write_test_code(test, code)
    write_sidecar(test)
    return test


def archive_test(test: Test):
    """Move the files to tests/_archive/ and mark the row archived. History
    stays; the file pair can be restored by moving it back and re-syncing."""
    cfg = appconfig.get_config()
    archive_dir = cfg.tests_dir / "_archive"
    archive_dir.mkdir(exist_ok=True)
    for path in (cfg.tests_dir / test.file_path,
                 sidecar_path(cfg.tests_dir / test.file_path)):
        if path.exists():
            os.replace(path, archive_dir / path.name)
    test.archived = True
    test.enabled = False
    test.save()

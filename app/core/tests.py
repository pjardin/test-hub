"""Unit tests for the logic that must not quietly regress: the schedule
due-math, the files<->DB sync, and the stats/ETA helpers.

    python manage.py test core          (or: ./app/dev.sh test)
"""
import copy
import io
import json
import shutil
import tempfile
import zipfile
from datetime import timedelta, datetime, time as dtime, timezone as dt_tz
from unittest import mock, skipUnless
from pathlib import Path

from django.test import TestCase
from django.utils import timezone as djtimezone

from core import appconfig
from core.models import Group, Run, Schedule, Test
from core.services import stats
from core.services.scheduler import due_slot, next_slot
from core.services.syncer import (create_test, id_from_stem, names_to_days,
                                  sync_from_files, write_groups_file)


class TempDataMixin:
    """Point the global config at a throwaway data dir for the duration."""

    def setUp(self):
        super().setUp()
        self._tmp = Path(tempfile.mkdtemp(prefix="testhub-test-"))
        raw = copy.deepcopy(appconfig.DEFAULTS)
        raw["paths"]["data_dir"] = str(self._tmp)
        self._old_config = appconfig._config
        appconfig._config = appconfig.Config(raw, self._tmp / "config.json")
        appconfig._config.ensure_dirs()

    def tearDown(self):
        appconfig._config = self._old_config
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    @property
    def tests_dir(self):
        return appconfig.get_config().tests_dir

    @property
    def cfg(self):
        return appconfig.get_config()


UTC = dt_tz.utc


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


class ScheduleDueTests(TestCase):
    """2026-08-03 is a Monday. America/Los_Angeles is UTC-7 in August."""

    def make(self, days=(0,), hh=9, mm=0, last_fired=None, enabled=True):
        sched = Schedule(time_of_day=dtime(hh, mm), enabled=enabled,
                         last_fired=last_fired)
        sched.days = list(days)
        return sched

    def test_fires_at_slot_time(self):
        s = self.make(days=[0], hh=9)  # Monday 09:00 PT == 16:00 UTC
        slot = due_slot(s, utc(2026, 8, 3, 16, 0), "America/Los_Angeles", 60)
        self.assertEqual(slot, utc(2026, 8, 3, 16, 0))

    def test_not_due_before_slot(self):
        s = self.make(days=[0], hh=9)
        self.assertIsNone(due_slot(s, utc(2026, 8, 3, 15, 59),
                                   "America/Los_Angeles", 60))

    def test_not_due_on_wrong_day(self):
        s = self.make(days=[1], hh=9)  # Tuesdays only
        self.assertIsNone(due_slot(s, utc(2026, 8, 3, 16, 30),
                                   "America/Los_Angeles", 60))

    def test_catchup_window_expires(self):
        s = self.make(days=[0], hh=9)
        # 3 hours late with a 60-minute window: missed, do not fire.
        self.assertIsNone(due_slot(s, utc(2026, 8, 3, 19, 0),
                                   "America/Los_Angeles", 60))

    def test_fires_within_catchup(self):
        s = self.make(days=[0], hh=9)
        slot = due_slot(s, utc(2026, 8, 3, 16, 45), "America/Los_Angeles", 60)
        self.assertEqual(slot, utc(2026, 8, 3, 16, 0))

    def test_last_fired_watermark_blocks_refire(self):
        s = self.make(days=[0], hh=9, last_fired=utc(2026, 8, 3, 16, 0))
        self.assertIsNone(due_slot(s, utc(2026, 8, 3, 16, 30),
                                   "America/Los_Angeles", 60))

    def test_disabled_never_fires(self):
        s = self.make(days=[0], hh=9, enabled=False)
        self.assertIsNone(due_slot(s, utc(2026, 8, 3, 16, 0),
                                   "America/Los_Angeles", 60))

    def test_next_slot_looks_ahead(self):
        s = self.make(days=[2], hh=9)  # Wednesdays
        nxt = next_slot(s, utc(2026, 8, 3, 16, 0), "America/Los_Angeles")
        self.assertEqual(nxt, utc(2026, 8, 5, 16, 0))


class SyncerTests(TempDataMixin, TestCase):
    def write_test_pair(self, stem, meta=None, code="def run(page, ctx):\n    pass\n"):
        (self.tests_dir / f"{stem}.py").write_text(code)
        if meta is not None:
            (self.tests_dir / f"{stem}.json").write_text(json.dumps(meta))

    def test_id_from_stem(self):
        self.assertEqual(id_from_stem("LOGIN-001__login_smoke"), "LOGIN-001")
        self.assertEqual(id_from_stem("PLAIN"), "PLAIN")

    def test_sync_creates_and_updates(self):
        self.write_test_pair("T-1__alpha", {"name": "Alpha", "tags": ["smoke"],
                                            "version": "2.0", "timeout_seconds": 60})
        summary = sync_from_files()
        self.assertIn("T-1", summary["created"])
        test = Test.objects.get(test_id="T-1")
        self.assertEqual(test.name, "Alpha")
        self.assertEqual(test.tags, ["smoke"])
        self.assertEqual(test.version_tag, "2.0")
        self.assertEqual(test.timeout_seconds, 60)

    def test_sidecar_autocreated_when_missing(self):
        self.write_test_pair("T-2__bare", meta=None)
        summary = sync_from_files()
        self.assertIn("T-2__bare.json", summary["sidecars_created"])
        self.assertTrue((self.tests_dir / "T-2__bare.json").exists())

    def test_archive_and_restore(self):
        self.write_test_pair("T-3__gone", {"name": "Gone"})
        sync_from_files()
        (self.tests_dir / "T-3__gone.py").unlink()
        summary = sync_from_files()
        self.assertIn("T-3", summary["archived"])
        self.assertTrue(Test.objects.get(test_id="T-3").archived)
        # file returns -> restored
        self.write_test_pair("T-3__gone", {"name": "Back"})
        summary = sync_from_files()
        self.assertIn("T-3", summary["restored"])
        self.assertFalse(Test.objects.get(test_id="T-3").archived)

    def test_schedules_from_sidecar(self):
        self.write_test_pair("T-4__sched", {
            "name": "S", "schedules": [{"days": ["mon", "fri"], "time": "07:30"}]})
        sync_from_files()
        sched = Test.objects.get(test_id="T-4").schedules.get()
        self.assertEqual(sched.days, [0, 4])
        self.assertEqual(sched.time_of_day, dtime(7, 30))

    def test_groups_file_roundtrip(self):
        self.write_test_pair("T-5__a", {"name": "A"})
        self.write_test_pair("T-6__b", {"name": "B"})
        (self.tests_dir / "_groups.json").write_text(json.dumps({
            "groups": [{"name": "Smoke", "tests": ["T-5", "T-6"],
                        "schedules": [{"days": ["sat"], "time": "02:00"}]}]}))
        sync_from_files()
        group = Group.objects.get(name="Smoke")
        self.assertEqual(group.tests.count(), 2)
        self.assertEqual(group.schedules.get().days, [5])
        # regenerating the file from the DB keeps the same content
        write_groups_file()
        data = json.loads((self.tests_dir / "_groups.json").read_text())
        self.assertEqual(data["groups"][0]["tests"], ["T-5", "T-6"])

    def test_create_test_writes_both_files(self):
        test = create_test("NEW-1", "Brand new", tags=["x"])
        self.assertTrue((self.tests_dir / test.file_path).exists())
        sidecar = json.loads(
            (self.tests_dir / test.file_path).with_suffix(".json").read_text())
        self.assertEqual(sidecar["id"], "NEW-1")
        self.assertEqual(sidecar["tags"], ["x"])
        # syncing again is a no-op, not a duplicate
        summary = sync_from_files()
        self.assertNotIn("NEW-1", summary["created"])

    def test_names_to_days(self):
        self.assertEqual(names_to_days(["mon", "Wednesday", "SUN"]), [0, 2, 6])


class StatsTests(TempDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.test = Test.objects.create(test_id="ST-1", file_path="ST-1__x.py")

    def add_run(self, status, duration):
        from django.utils import timezone as djtz
        Run.objects.create(test=self.test, status=status,
                           duration_seconds=duration,
                           finished_at=djtz.now())

    def test_estimate_none_without_history(self):
        self.assertIsNone(stats.estimate_seconds(self.test))

    def test_estimate_averages_recent_runs(self):
        for d in (10, 20, 30):
            self.add_run(Run.PASSED, d)
        self.assertAlmostEqual(stats.estimate_seconds(self.test), 20.0)

    def test_killed_runs_do_not_pollute_estimate(self):
        self.add_run(Run.PASSED, 10)
        self.add_run(Run.KILLED, 1)      # should be ignored
        self.assertAlmostEqual(stats.estimate_seconds(self.test), 10.0)

    def test_flakiness(self):
        self.assertIsNone(stats.flakiness([]))
        self.assertEqual(stats.flakiness(["passed", "passed", "passed"]), 0)
        self.assertEqual(stats.flakiness(["passed", "failed", "passed"]), 1.0)

    def test_percentile(self):
        self.assertEqual(stats.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertIsNone(stats.percentile([], 95))

    def test_test_stats_pass_rate(self):
        for status in (Run.PASSED, Run.PASSED, Run.FAILED):
            self.add_run(status, 5)
        s = stats.test_stats(self.test)
        self.assertEqual(s["total_runs"], 3)
        self.assertAlmostEqual(s["pass_rate"], 66.666, places=1)


class ScheduleBrowserRoundtripTests(TempDataMixin, TestCase):
    def test_sidecar_browser_roundtrip(self):
        (self.tests_dir / "B-1__b.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / "B-1__b.json").write_text(json.dumps({
            "name": "B", "schedules": [
                {"days": ["mon"], "time": "08:00", "browser": "msedge"}]}))
        sync_from_files()
        sched = Test.objects.get(test_id="B-1").schedules.get()
        self.assertEqual(sched.browser, "msedge")
        # regenerate the sidecar from the DB: browser survives
        from core.services.syncer import sidecar_dict
        data = sidecar_dict(Test.objects.get(test_id="B-1"))
        self.assertEqual(data["schedules"][0]["browser"], "msedge")


class RemoteRecorderCodegenTests(TestCase):
    def test_build_code_renders_steps(self):
        from core.services.remote_recorder import build_code
        code = build_code([
            {"action": "fill", "selector": "#name", "value": 'Ada "L"'},
            {"action": "click", "selector": 'button:has-text("Submit")'},
            {"action": "select", "selector": "#priority", "value": "high"},
            {"action": "press", "selector": "#q", "value": "Enter"},
        ], "http://target/")
        self.assertIn("def run(page, ctx):", code)
        self.assertIn("page.goto(ctx.base_url)", code)
        self.assertIn('page.fill("#name", "Ada \\"L\\"")', code)
        self.assertIn('page.click("button:has-text(\\"Submit\\")")', code)
        self.assertIn('page.select_option("#priority", "high")', code)
        self.assertIn('page.press("#q", "Enter")', code)

    def test_build_code_empty(self):
        from core.services.remote_recorder import build_code
        code = build_code([], "http://target/")
        self.assertIn("no interactions were recorded", code)


class TimingTests(TempDataMixin, TestCase):
    def test_timed_page_records_actions(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "harness_mod", Path(__file__).parent / "harness" / "run_test.py")
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)

        class FakePage:
            def click(self, selector):
                return "clicked " + selector
            def title(self):
                return "T"
            some_attr = 42

        ctx = harness.Ctx("http://x/", self._tmp, "T-1")
        page = harness.TimedPage(FakePage(), ctx)
        self.assertEqual(page.click("#go"), "clicked #go")
        self.assertEqual(page.title(), "T")
        self.assertEqual(page.some_attr, 42)     # passthrough
        names = [t["name"] for t in ctx.timings]
        self.assertEqual(names, ["click #go", "title"])
        with ctx.timed("my phase"):
            pass
        self.assertEqual(ctx.timings[-1]["name"], "my phase")
        self.assertEqual(ctx.timings[-1]["op"], "block")

    def test_timing_trends_matches_steps_across_runs(self):
        import json as j
        from django.utils import timezone as djtz
        test = Test.objects.create(test_id="TT-1", file_path="TT-1__x.py")
        for ms in (100, 200, 300):
            Run.objects.create(
                test=test, status=Run.PASSED, duration_seconds=1,
                finished_at=djtz.now(),
                timings_json=j.dumps([
                    {"name": "goto /", "op": "goto", "ms": ms, "t": 0.1},
                    {"name": "click #a", "op": "click", "ms": ms / 10, "t": 0.5},
                ]))
        trends = stats.timing_trends(test)
        self.assertEqual(len(trends["labels"]), 3)
        names = {s["name"] for s in trends["series"]}
        self.assertEqual(names, {"goto /", "click #a"})
        goto = next(s for s in trends["series"] if s["name"] == "goto /")
        self.assertEqual(goto["data"], [100, 200, 300])


class RecorderHandoffTests(TestCase):
    def test_append_steps_into_run_body(self):
        from core.services.remote_recorder import append_steps_to_code
        code = ('"""Doc."""\n\n\ndef run(page, ctx):\n'
                '    page.goto(ctx.base_url)\n'
                '    assert page.title()\n\n\n'
                'def helper():\n    return 1\n')
        out = append_steps_to_code(code, [
            {"action": "click", "selector": "#next"},
            {"action": "fill", "selector": "#q", "value": "x"},
        ])
        self.assertIn("# --- appended from a recording ---", out)
        # appended INSIDE run(), before the helper function
        self.assertLess(out.index('page.click("#next")'), out.index("def helper"))
        self.assertIn('    page.fill("#q", "x")', out)

    def test_append_without_run_returns_none(self):
        from core.services.remote_recorder import append_steps_to_code
        self.assertIsNone(append_steps_to_code("x = 1\n", [
            {"action": "click", "selector": "#a"}]))

    def test_extract_prior_steps(self):
        from core.services.remote_recorder import extract_prior_steps
        steps = extract_prior_steps(
            "def run(page, ctx):\n"
            "    page.goto(ctx.base_url)\n"
            "    with ctx.timed(\"phase\"):\n"
            "        page.click(\"#a\")\n"
            "    x = 1\n")
        self.assertEqual(steps, ["page.goto(ctx.base_url)",
                                 'with ctx.timed("phase"):'.rstrip(":") + ":",
                                 'page.click("#a")'])


class HousekeepingTests(TempDataMixin, TestCase):
    def test_prune_removes_only_old_run_dirs(self):
        import os, time as _t
        from core.services.housekeeping import prune_results, results_usage
        cfg = appconfig.get_config()
        old_dir = cfg.results_dir / "T-1" / "20200101-000000-r1"
        new_dir = cfg.results_dir / "T-1" / "now-r2"
        for d in (old_dir, new_dir):
            d.mkdir(parents=True)
            (d / "video.webm").write_bytes(b"x" * 1000)
        stale = _t.time() - 90 * 86400
        os.utime(old_dir / "video.webm", (stale, stale))

        usage = results_usage()
        self.assertEqual(usage["run_dirs"], 2)

        result = prune_results(30)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(old_dir.exists())
        self.assertTrue(new_dir.exists())

    def test_prune_zero_days_is_noop(self):
        from core.services.housekeeping import prune_results
        self.assertEqual(prune_results(0), {"removed": 0, "freed_bytes": 0})


class UrlPrefixConfigTests(TestCase):
    def test_prefix_normalisation(self):
        import copy
        from core import appconfig
        for raw, want in (("", ""), ("/", ""), ("testhub", "/testhub"),
                          ("/testhub", "/testhub"), ("testhub/", "/testhub"),
                          ("/testhub/", "/testhub")):
            base = copy.deepcopy(appconfig.DEFAULTS)
            base["site"]["url_prefix"] = raw
            cfg = appconfig.Config(base, appconfig.HOME_DIR / "x.json")
            self.assertEqual(cfg.url_prefix, want, raw)
        base = copy.deepcopy(appconfig.DEFAULTS)
        base["site"]["url_prefix"] = "/testhub"
        cfg = appconfig.Config(base, appconfig.HOME_DIR / "x.json")
        self.assertTrue(cfg.target_url.endswith("/testhub/demo/"))


class PointAndCheckTests(TestCase):
    def test_check_and_wait_codegen(self):
        from core.services.remote_recorder import build_code, needs_expect
        steps = [
            {"action": "assert_text", "selector": "#r", "value": "Saved"},
            {"action": "assert_value", "selector": "#n", "value": "Ada"},
            {"action": "assert_visible", "selector": "#p"},
            {"action": "assert_css", "selector": "h1", "extra": "color",
             "value": "rgb(1, 2, 3)"},
            {"action": "wait_visible", "selector": "#done", "timeout_ms": 60000},
            {"action": "wait_hidden", "selector": "#spin", "timeout_ms": 5000},
            {"action": "wait_text", "selector": "#s", "value": "OK", "timeout_ms": 120000},
        ]
        self.assertTrue(needs_expect(steps))
        code = build_code(steps, "http://x/")
        self.assertIn("from playwright.sync_api import expect", code)
        self.assertIn('to_contain_text("Saved")', code)
        self.assertIn('to_have_value("Ada")', code)
        self.assertIn("to_be_visible()", code)
        self.assertIn('to_have_css("color", "rgb(1, 2, 3)")', code)
        self.assertIn('state="visible", timeout=60000', code)
        self.assertIn('state="hidden", timeout=5000', code)
        self.assertIn('to_contain_text("OK", timeout=120000)', code)
        # no checks -> no import
        self.assertNotIn("import expect",
                         build_code([{"action": "click", "selector": "#a"}], "u"))

    def test_manual_step_validation(self):
        from core.services.remote_recorder import RecorderSession
        session = RecorderSession.__new__(RecorderSession)  # no thread
        import threading
        session.lock = threading.Lock()
        session.steps = []
        self.assertTrue(session.add_manual_step(
            {"action": "wait_visible", "selector": "#x", "timeout_ms": 9e9}))
        self.assertEqual(session.steps[0]["timeout_ms"], 4 * 3600 * 1000)  # capped
        self.assertFalse(session.add_manual_step({"action": "rm_rf", "selector": "#x"}))


class NonTechnicalHelpersTests(TestCase):
    def test_error_short_strips_call_log_and_traceback(self):
        from core.views import error_short
        msg = ("Locator expected to contain text 'Saved'\n"
               "Actual value: Error occurred\n"
               "Call log:\n  - waiting for locator...\n  - 50 retries")
        short = error_short(msg)
        self.assertIn("expected to contain", short)
        self.assertNotIn("Call log", short)
        msg2 = "AssertionError: nope\nTraceback (most recent call last):\n  File x"
        self.assertEqual(error_short(msg2), "AssertionError: nope")
        self.assertEqual(error_short(""), "")

    def test_humanize_code(self):
        from core.services.humanize import humanize_code
        steps = humanize_code(
            'def run(page, ctx):\n'
            '    page.goto(ctx.base_url)\n'
            '    page.fill("#a", "x")\n'
            '    expect(page.locator("#r")).to_contain_text("OK", timeout=30000)\n'
            '    mystery(1)\n')
        texts = [s["text"] for s in steps]
        self.assertEqual(texts[0], "Open the app's page")
        self.assertIn("Type “x” into #a", texts)
        self.assertIn("Check #r says “OK” (up to 30 s)", texts)
        self.assertEqual(steps[-1]["kind"], "code")


class ReelPlayerPresenceTests(TestCase):
    """The activity replay player was once deleted by an unrelated cleanup
    (adjacent block in app.js). Cheap guard so it cannot vanish silently."""

    def test_app_js_still_has_the_reel_player(self):
        from pathlib import Path
        js = (Path(__file__).parent / "static" / "core" / "app.js").read_text()
        for marker in ("frames-data", "reel-img", "reel-play", "skipped "):
            self.assertIn(marker, js, f"activity replay player lost: {marker}")


class AnalyticsTests(TempDataMixin, TestCase):
    def _run(self, test, status, **kw):
        from django.utils import timezone as djtz
        return Run.objects.create(test=test, status=status,
                                  finished_at=djtz.now(), **kw)

    def test_error_signature_normalises_noise(self):
        sig = stats.error_signature
        a = sig("Timeout 30000ms exceeded waiting for /tmp/abc/x\nsecond line")
        b = sig("Timeout 5000ms exceeded waiting for /tmp/zzz/x\nother line")
        self.assertEqual(a, b)  # same failure, different numbers/paths
        self.assertNotEqual(a, sig("Element not found"))
        self.assertEqual(sig(""), "")

    def test_error_clusters_group_across_tests(self):
        t1 = Test.objects.create(test_id="C-1", file_path="a.py")
        t2 = Test.objects.create(test_id="C-2", file_path="b.py")
        for t in (t1, t2):
            self._run(t, Run.FAILED, error_message="Timeout 1000ms exceeded\nx")
        self._run(t1, Run.FAILED, error_message="Totally different problem")
        clusters = stats.error_clusters()
        self.assertEqual(clusters[0]["count"], 2)
        self.assertEqual(clusters[0]["tests"], ["C-1", "C-2"])

    def test_new_vs_chronic_split(self):
        from datetime import timedelta
        from django.utils import timezone as djtz
        fresh = Test.objects.create(test_id="N-1", file_path="n.py")
        old = Test.objects.create(test_id="O-1", file_path="o.py")
        self._run(fresh, Run.FAILED)
        r = self._run(old, Run.FAILED)
        Run.objects.filter(pk=r.pk).update(queued_at=djtz.now() - timedelta(days=10))
        split = stats.new_vs_chronic(new_within_days=2)
        self.assertEqual([r["test_id"] for r in split["new"]], ["N-1"])
        self.assertEqual([r["test_id"] for r in split["chronic"]], ["O-1"])

    def test_duration_histogram_and_health(self):
        t = Test.objects.create(test_id="H-1", file_path="h.py")
        for d in (1.0, 1.1, 1.2, 9.0):
            self._run(t, Run.PASSED, duration_seconds=d)
        hist = stats.duration_histogram(t, buckets=4)
        self.assertEqual(sum(hist["counts"]), 4)
        self.assertEqual(hist["counts"][0], 3)   # cluster of fast runs
        Test.objects.create(test_id="NEVER-1", file_path="x.py")
        health = stats.suite_health()
        self.assertIn("NEVER-1", health["never_run"])

    def test_slowest_steps_and_heatmap_shape(self):
        import json as j
        t = Test.objects.create(test_id="S-1", file_path="s.py")
        self._run(t, Run.PASSED, duration_seconds=1,
                  timings_json=j.dumps([{"name": "click #a", "ms": 500, "t": 0.1},
                                        {"name": "goto /", "ms": 100, "t": 0}]))
        steps = stats.slowest_steps()
        self.assertEqual(steps[0]["step"], "click #a")
        self.assertEqual(steps[0]["avg_ms"], 500)
        heat = stats.failure_heatmap()
        self.assertEqual(len(heat["rows"]), 7)
        self.assertEqual(len(heat["rows"][0]["cells"]), len(stats.HOUR_BUCKETS))


class BackupTransferTests(TempDataMixin, TestCase):
    def test_backup_contains_tests_and_db_and_restores(self):
        import io, zipfile
        from core.services import backup as bk
        (self.tests_dir / "BK-1__x.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / "BK-1__x.json").write_text('{"id": "BK-1", "name": "X"}')
        data = bk.build_backup()
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("BACKUP.json", names)
        self.assertIn("tests/BK-1__x.py", names)

        path = self._tmp / "b.zip"
        path.write_bytes(data)
        (self.tests_dir / "BK-1__x.py").unlink()          # lose a file
        report = bk.restore_backup(path, restore_db=False)
        self.assertEqual(report["tests"], 2)
        self.assertTrue((self.tests_dir / "BK-1__x.py").exists())
        self.assertTrue(Path(report["kept_at"]).exists())  # nothing deleted

    def test_export_import_roundtrip(self):
        import zipfile
        from core.services import backup as bk
        (self.tests_dir / "EX-1__a.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / "EX-1__a.json").write_text('{"id": "EX-1"}')
        (self.tests_dir / "EX-2__b.py").write_text("def run(page, ctx):\n    pass\n")
        blob = bk.export_tests(["EX-1"])
        import io as _io
        names = zipfile.ZipFile(_io.BytesIO(blob)).namelist()
        self.assertIn("EX-1__a.py", names)
        self.assertNotIn("EX-2__b.py", names)             # selective export

        path = self._tmp / "t.zip"
        path.write_bytes(blob)
        for f in self.tests_dir.glob("EX-1*"):
            f.unlink()
        report = bk.import_tests(path)
        self.assertIn("EX-1__a.py", report["written"])
        self.assertTrue((self.tests_dir / "EX-1__a.py").exists())

    def test_import_rejects_paths_and_bad_json(self):
        import zipfile
        from core.services import backup as bk
        path = self._tmp / "evil.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../../etc/passwd", "x")
            zf.writestr("sub/dir/T.py", "x")
            zf.writestr("BAD-1__x.json", "{not json")
            zf.writestr("OK-1__x.py", "def run(page, ctx):\n    pass\n")
        report = bk.import_tests(path)
        self.assertEqual(report["written"], ["OK-1__x.py"])
        self.assertEqual(len(report["errors"]), 3)
        self.assertFalse((self.tests_dir / "passwd").exists())


class DiagnosticsTests(TempDataMixin, TestCase):
    def test_quick_checks_shape(self):
        from core.services import diagnostics
        checks = diagnostics.run_checks(quick=True)
        names = {c["name"] for c in checks}
        for expected in ("Python", "Database", "Disk space", "Tests folder",
                         "Schedule timezone", "Test Hub version"):
            self.assertIn(expected, names)
        for c in checks:
            self.assertIn(c["status"], ("ok", "warn", "fail"))
        s = diagnostics.summary(checks)
        self.assertEqual(s["ok"] + s["warn"] + s["fail"], len(checks))


class PasswordGateTests(TempDataMixin, TestCase):
    def _set_password(self, value):
        appconfig.get_config().raw["site"]["password"] = value

    def test_no_password_means_open(self):
        self._set_password("")
        self.assertEqual(self.client.get("/tests/").status_code, 200)

    def test_password_redirects_then_allows(self):
        self._set_password("s3cret")
        try:
            resp = self.client.get("/tests/")
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/login/", resp["Location"])
            self.assertEqual(self.client.post("/login/", {"password": "nope"}).status_code, 200)
            resp = self.client.post("/login/", {"password": "s3cret"})
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(self.client.get("/tests/").status_code, 200)
        finally:
            self._set_password("")


class DeleteAndStorageTests(TempDataMixin, TestCase):
    def _run_with_files(self, test, name="r1", size=1000):
        from django.utils import timezone as djtz
        cfg = appconfig.get_config()
        run = Run.objects.create(test=test, status=Run.PASSED, duration_seconds=1,
                                 finished_at=djtz.now(),
                                 artifacts_rel=f"{test.test_id}/{name}")
        base = cfg.results_dir / run.artifacts_rel
        (base / "screenshots").mkdir(parents=True)
        (base / "video.webm").write_bytes(b"x" * size)
        (base / "screenshots" / "01.png").write_bytes(b"y" * size)
        return run

    def test_delete_artifacts_keeps_history(self):
        from core.services.housekeeping import delete_run_artifacts
        cfg = appconfig.get_config()
        test = Test.objects.create(test_id="D-1", file_path="d.py")
        run = self._run_with_files(test)
        freed = delete_run_artifacts(run)
        self.assertGreaterEqual(freed, 2000)
        self.assertFalse((cfg.results_dir / run.artifacts_rel).exists())
        self.assertTrue(Run.objects.filter(pk=run.pk).exists())   # charts survive

    def test_delete_whole_run_removes_row(self):
        from core.services.housekeeping import delete_run
        test = Test.objects.create(test_id="D-2", file_path="d.py")
        run = self._run_with_files(test)
        delete_run(run)
        self.assertFalse(Run.objects.filter(pk=run.pk).exists())

    def test_purge_test_keeps_newest(self):
        from core.services.housekeeping import purge_test
        cfg = appconfig.get_config()
        test = Test.objects.create(test_id="D-3", file_path="d.py")
        runs = [self._run_with_files(test, name=f"r{i}") for i in range(4)]
        result = purge_test(test, keep_last=2)
        self.assertEqual(result["runs"], 2)
        surviving = [r for r in runs if (cfg.results_dir / r.artifacts_rel).exists()]
        self.assertEqual(len(surviving), 2)
        self.assertEqual(Run.objects.filter(test=test).count(), 4)  # history intact

    def test_purge_all_status_filter(self):
        from core.services.housekeeping import purge_all
        from django.utils import timezone as djtz
        cfg = appconfig.get_config()
        test = Test.objects.create(test_id="D-4", file_path="d.py")
        passed = self._run_with_files(test, name="p1")
        failed = self._run_with_files(test, name="f1")
        Run.objects.filter(pk=failed.pk).update(status=Run.FAILED)
        purge_all(status=Run.PASSED)
        self.assertFalse((cfg.results_dir / passed.artifacts_rel).exists())
        self.assertTrue((cfg.results_dir / failed.artifacts_rel).exists())

    def test_storage_disabled_by_default(self):
        from core.services import storage
        self.assertFalse(storage.enabled())
        self.assertFalse(storage.check()["ok"])
        run = self._run_with_files(Test.objects.create(test_id="S-9", file_path="s.py"))
        self.assertTrue(storage.upload_run(run).get("skipped"))   # no-op, no crash


class HealthAndGateTests(TempDataMixin, TestCase):
    def test_health_endpoint_leaks_nothing(self):
        body = self.client.get("/api/health/").json()
        self.assertEqual(set(body), {"ok", "service", "runner"})

    def test_status_feed_is_gated_but_health_is_not(self):
        cfg = appconfig.get_config()
        cfg.raw["site"]["password"] = "s3cret"
        try:
            self.assertEqual(self.client.get("/api/health/").status_code, 200)
            # the rich feed names tests and batches -- it must be protected
            self.assertEqual(self.client.get("/api/status/").status_code, 302)
            self.assertEqual(self.client.get("/tests/").status_code, 302)
        finally:
            cfg.raw["site"]["password"] = ""


class FailureMessageTests(TestCase):
    """Messages shown to non-technical testers must read like English --
    an internal exception name once leaked through as '_killed'."""

    def _harness_source(self):
        from pathlib import Path
        return (Path(__file__).parent / "harness" / "run_test.py").read_text()

    def test_harness_kill_message_is_human(self):
        src = self._harness_source()
        self.assertNotIn("exc.__class__.__name__", src)
        self.assertIn("stopped by request (Kill)", src)

    def test_timeout_message_says_what_to_do_about_it(self):
        """Forgetting to raise timeout_seconds is THE mistake people make
        writing a test that waits for a long job -- the default is 300s, and
        a bare "exceeded its timeout" does not hint at the fix."""
        src = self._harness_source()
        self.assertIn("Timeout seconds", src,
                      "the timeout message should name the field to change")
        self.assertNotIn('f"test exceeded its {args.timeout}s timeout"', src)

    def test_error_short_strips_the_traceback_wall(self):
        from core.views import error_short
        self.assertEqual(error_short(""), "")
        msg = ('the test ran longer than its 25s time limit and was stopped.\n'
               'Call log:\n  - waiting for locator("#x")')
        self.assertNotIn("Call log", error_short(msg))
        self.assertIn("25s time limit", error_short(msg))
        tb = "AssertionError: nope\nTraceback (most recent call last)\n  File ..."
        self.assertEqual(error_short(tb).strip(), "AssertionError: nope")


class SecurityRegressionTests(TempDataMixin, TestCase):
    """Every one of these was a REAL hole found by review. Each test fails
    against the code as it was before the fix."""

    def _password(self, value):
        appconfig.get_config().raw["site"]["password"] = value

    def test_static_substring_cannot_bypass_the_gate(self):
        # /artifacts/static/../<run>/run.log used to be exempted because the
        # check was `"/static/" in path`, leaking every artifact.
        self._password("s3cret")
        try:
            for path in ("/artifacts/static/../x/run.log",
                         "/artifacts/static/%2e%2e/x/run.log",
                         "/tests/login/",
                         "/tests/x/api/health/"):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 302,
                                 f"{path} was NOT gated (status {resp.status_code})")
            self.assertEqual(self.client.get("/static/core/style.css").status_code, 200)
            self.assertEqual(self.client.get("/api/health/").status_code, 200)
        finally:
            self._password("")

    def test_login_next_cannot_redirect_off_site(self):
        self._password("s3cret")
        try:
            resp = self.client.post("/login/?next=https://evil.example.com/x",
                                    {"password": "s3cret"})
            self.assertEqual(resp.status_code, 302)
            self.assertNotIn("evil.example.com", resp["Location"])
            resp = self.client.post("/login/?next=/metrics/", {"password": "s3cret"})
            self.assertEqual(resp["Location"], "/metrics/")
        finally:
            self._password("")

    def test_restore_rejects_zip_slip(self):
        import zipfile
        from core.services import backup as bk
        cfg = appconfig.get_config()
        outside = cfg.data_dir.parent / "PWNED.txt"
        outside.unlink(missing_ok=True)
        path = self._tmp / "evil-backup.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("BACKUP.json", "{}")
            zf.writestr("tests/../../PWNED.txt", "owned")
            zf.writestr("tests/GOOD-1__x.py", "def run(page, ctx):\n    pass\n")
        report = bk.restore_backup(path, restore_db=False)
        self.assertFalse(outside.exists(), "zip-slip wrote outside the data dir")
        self.assertEqual(report["tests"], 1)
        self.assertTrue((cfg.tests_dir / "GOOD-1__x.py").exists())

    def test_artifact_traversal_is_rejected_before_s3(self):
        cfg = appconfig.get_config()
        cfg.raw["storage"]["backend"] = "s3"
        try:
            self.assertEqual(self.client.get("/artifacts/../../etc/passwd").status_code, 404)
        finally:
            cfg.raw["storage"]["backend"] = "local"


class RunnerRegressionTests(TempDataMixin, TestCase):
    def test_kill_is_recorded_as_killed_not_error(self):
        """The supervision loop polls the flag every 2s but the process is
        signalled immediately, so a fast-exiting harness was filed as ERROR."""
        from pathlib import Path
        src = (Path(__file__).parent / "services" / "runner.py").read_text()
        self.assertIn('result.get("status") == "killed"', src)

    def test_kill_run_uses_conditional_updates(self):
        """A bare save() of a stale row could resurrect a finished run or
        wipe artifacts_rel from one that had just started."""
        import inspect
        from core.services import runner as runner_mod
        src = inspect.getsource(runner_mod.kill_run)
        self.assertIn("update(kill_requested=True)", src)
        self.assertIn("status=Run.QUEUED", src)
        self.assertNotIn("run.save()", src)

    def test_killing_a_queued_run_is_atomic(self):
        from django.utils import timezone as djtz
        from core.services import runner as runner_mod
        test = Test.objects.create(test_id="K-1", file_path="k.py")
        run = Run.objects.create(test=test, status=Run.QUEUED)
        self.assertTrue(runner_mod.kill_run(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, Run.KILLED)
        self.assertIn("Kill", run.error_message)
        # a finished run cannot be killed (and is not resurrected)
        done = Run.objects.create(test=test, status=Run.PASSED,
                                  duration_seconds=2, finished_at=djtz.now())
        self.assertFalse(runner_mod.kill_run(done.pk))
        done.refresh_from_db()
        self.assertEqual(done.status, Run.PASSED)

    def test_cli_does_not_abort_server_runs(self):
        """`manage.py runtest` used to call recover_orphans(), which marks
        every active run ABORTED -- wiping runs a live server had queued.
        (Measured before the fix: one CLI call aborted two in-flight runs.)"""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "management" / "commands" / "runtest.py").read_text()
        calls = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", getattr(n.func, "id", "")) == "recover_orphans"]
        self.assertEqual(calls, [], "runtest must not run orphan recovery")

    def test_supervision_uses_monotonic_clock(self):
        from pathlib import Path
        src = (Path(__file__).parent / "services" / "runner.py").read_text()
        self.assertIn("started_monotonic", src)
        self.assertIn("proc.wait(timeout=30)", src)


class SchedulerRegressionTests(TestCase):
    def _sched(self, days, hh, mm=0, last=None):
        s = Schedule(time_of_day=dtime(hh, mm), enabled=True, last_fired=last)
        s.days = list(days)
        return s

    def test_late_night_slot_survives_midnight(self):
        """23:30 Monday, hub restarted 23:25 and back at 00:05 Tuesday: the
        Monday slot must still fire inside the catch-up window."""
        s = self._sched([0], 23, 30)                       # Monday 23:30 PT
        now = utc(2026, 8, 4, 7, 5)                        # Tue 00:05 PT
        slot = due_slot(s, now, "America/Los_Angeles", 60)
        self.assertIsNotNone(slot, "the Monday 23:30 slot was lost at midnight")
        self.assertEqual(slot, utc(2026, 8, 4, 6, 30))     # Mon 23:30 PT

    def test_no_double_fire_after_midnight_catchup(self):
        s = self._sched([0], 23, 30, last=utc(2026, 8, 4, 6, 30))
        self.assertIsNone(due_slot(s, utc(2026, 8, 4, 7, 5),
                                   "America/Los_Angeles", 60))

    def test_spring_forward_slot_still_fires(self):
        """02:30 does not exist locally on the US spring-forward day. With
        the old naive comparison the slot could be skipped entirely; now it
        resolves to a real UTC instant (03:30 local) and fires there."""
        s = self._sched([6], 2, 30)                        # Sunday 02:30
        slot = due_slot(s, utc(2026, 3, 8, 10, 35), "America/Los_Angeles", 60)
        self.assertIsNotNone(slot, "the 02:30 slot vanished on the DST day")
        self.assertEqual(slot, utc(2026, 3, 8, 10, 30))    # = 03:30 local
        # and not before it is due
        self.assertIsNone(due_slot(s, utc(2026, 3, 8, 10, 2),
                                   "America/Los_Angeles", 60))

    def test_fall_back_does_not_double_fire(self):
        """The ambiguous 01:30 happens twice on the fall-back day; the
        watermark must stop the second one."""
        s = self._sched([6], 1, 30)                        # Sunday 01:30
        first = due_slot(s, utc(2026, 11, 1, 8, 35), "America/Los_Angeles", 60)
        self.assertIsNotNone(first)
        s.last_fired = first
        self.assertIsNone(due_slot(s, utc(2026, 11, 1, 9, 35),
                                   "America/Los_Angeles", 60))


class StatsRegressionTests(TempDataMixin, TestCase):
    def test_empty_error_message_does_not_break_metrics(self):
        """A run can finish FAILED with no message; that used to 500 the
        whole metrics page (and every test page) via splitlines()[0]."""
        from django.utils import timezone as djtz
        test = Test.objects.create(test_id="E-1", file_path="e.py")
        Run.objects.create(test=test, status=Run.FAILED, error_message="",
                           finished_at=djtz.now(), duration_seconds=1)
        clusters = stats.error_clusters()
        self.assertEqual(len(clusters), 1)
        self.assertIn("no message", clusters[0]["sample"])
        self.assertEqual(stats.error_signature(""), "")
        self.assertEqual(self.client.get("/metrics/").status_code, 200)
        self.assertEqual(self.client.get("/tests/E-1/").status_code, 200)


class HousekeepingRegressionTests(TempDataMixin, TestCase):
    """Deleting run data used to be O(runs) queries and could hide artifacts."""

    def _runs(self, n, with_files=True):
        test = Test.objects.create(test_id="P-1", file_path="p.py")
        made = []
        for i in range(n):
            rel = f"P-1/run{i}"
            run = Run.objects.create(test=test, status=Run.PASSED,
                                     artifacts_rel=rel, finished_at=djtimezone.now())
            if with_files:
                d = self.cfg.results_dir / rel
                d.mkdir(parents=True, exist_ok=True)
                (d / "run.log").write_text("x" * 100)
            made.append(run)
        return test, made

    def test_purge_uses_bulk_queries(self):
        from core.services.housekeeping import purge_test
        test, _ = self._runs(25)
        # Two, regardless of how many runs: one SELECT and one bulk UPDATE.
        # It was 2 *per run* -- 20k statements to purge 10k runs, holding
        # SQLite's write lock long enough to stall the runner.
        with self.assertNumQueries(2):
            result = purge_test(test, artifacts_only=True)
        self.assertEqual(result["runs"], 25)
        self.assertGreater(result["freed_bytes"], 0)
        self.assertFalse((self.cfg.results_dir / "P-1/run0").exists())
        self.assertEqual(Run.objects.count(), 25)      # rows kept

    def test_purge_whole_runs_deletes_rows(self):
        from core.services.housekeeping import purge_all
        self._runs(10)
        purge_all(artifacts_only=False)
        self.assertEqual(Run.objects.count(), 0)

    def test_keep_last_protects_newest(self):
        from core.services.housekeeping import purge_test
        test, made = self._runs(5)
        purge_test(test, artifacts_only=True, keep_last=2)
        surviving = [r for r in made
                     if (self.cfg.results_dir / r.artifacts_rel).exists()]
        self.assertEqual(len(surviving), 2)

    def test_results_usage_is_cached_and_invalidated(self):
        from core.services import housekeeping as hk
        test, made = self._runs(3)
        first = hk.results_usage()
        self.assertGreater(first["bytes"], 0)
        # A file appearing behind the cache is not re-walked ...
        (self.cfg.results_dir / "P-1/run0/extra.bin").write_bytes(b"y" * 5000)
        self.assertEqual(hk.results_usage()["bytes"], first["bytes"])
        # ... but a delete invalidates it, so the page never shows a stale total.
        hk.delete_run_artifacts(made[0])
        self.assertLess(hk.results_usage()["bytes"], first["bytes"])
        self.assertEqual(hk.results_usage(max_age=0)["bytes"],
                         hk.results_usage()["bytes"])


class ApiInputTests(TempDataMixin, TestCase):
    """Every POST endpoint must survive a body that is not the shape it wants:
    these are reachable from any logged-in tab, and a 500 here used to take a
    stack trace into the log and an unhelpful error to the tester."""

    BODIES = [b"", b"[]", b'"hello"', b"null", b"{bad json", b"123"]

    def test_malformed_bodies_never_500(self):
        Test.objects.create(test_id="A-1", file_path="a.py")
        # Resolved from urls.py by NAME -- hardcoding paths made an earlier
        # version of this test pass vacuously against URLs that 404'd.
        from django.urls import reverse
        endpoints = [reverse("api_run_tests"),
                     reverse("api_purge_all"),
                     reverse("api_purge_test", args=["A-1"]),
                     reverse("api_prune_results"),
                     reverse("api_remote_rec_start"),
                     reverse("api_remote_rec_add_step"),
                     reverse("api_remote_rec_input")]
        for url in endpoints:
            for body in self.BODIES:
                resp = self.client.post(url, data=body,
                                        content_type="application/json")
                self.assertNotEqual(resp.status_code, 404,
                                    f"{url} does not exist -- test is vacuous")
                self.assertLess(resp.status_code, 500,
                                f"{url} 500'd on body {body!r}")

    def test_int_arg_coercion(self):
        from core.api import _int_arg
        self.assertEqual(_int_arg({"n": "7"}, "n"), 7)
        self.assertEqual(_int_arg({"n": -3}, "n"), 0)        # clamped
        self.assertEqual(_int_arg({"n": "abc"}, "n", 5), 5)  # default on junk
        self.assertEqual(_int_arg({"n": None}, "n", 5), 5)
        self.assertEqual(_int_arg({}, "n", 30), 30)

    def test_body_helper_always_returns_dict(self):
        from core.api import _body

        class FakeReq:
            def __init__(self, b): self.body = b
        for raw in self.BODIES:
            self.assertIsInstance(_body(FakeReq(raw)), dict)


class ShutdownAndTeardownTests(TempDataMixin, TestCase):
    def test_shutdown_aborts_inflight_runs(self):
        """Ctrl-C used to leave runs 'running' forever and orphan Chromium."""
        from core.services.runner import Runner
        test = Test.objects.create(test_id="S-1", file_path="s.py")
        Run.objects.create(test=test, status=Run.RUNNING, pid=None)
        Run.objects.create(test=test, status=Run.QUEUED)
        n = Runner(self.cfg).shutdown(grace=0)
        self.assertEqual(n, 2)
        self.assertEqual(Run.objects.filter(status=Run.ABORTED).count(), 2)
        self.assertIn("shut down", Run.objects.first().error_message)

    def test_terminate_signals_harness_before_the_group(self):
        """The harness saves video/result.json from its SIGTERM handler; if we
        flatten the group first, Chromium dies mid-teardown and the video is
        lost. Assert the harness pid is signalled on its own, first."""
        import signal as sig
        from core.services import runner as rmod
        calls = []

        class FakeProc:
            pid = 4242
            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                return 0

        with mock.patch.object(rmod.os, "kill",
                               side_effect=lambda p, s: calls.append(("kill", p, s))), \
             mock.patch.object(rmod.os, "killpg",
                               side_effect=lambda p, s: calls.append(("killpg", p, s))), \
             mock.patch.object(rmod.os, "getpgid", return_value=4242):
            rmod._terminate_group(FakeProc(), "test")

        self.assertEqual(calls[0], ("kill", 4242, sig.SIGTERM),
                         f"harness must be signalled alone first, got {calls}")
        self.assertTrue(any(c[0] == "killpg" for c in calls),
                        "group must still be swept for Chromium leftovers")

    def test_video_scratch_dir_is_always_removed(self):
        """_video_tmp holds a partial recording; the harness only cleaned it
        on the success path, so every killed run left one behind."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "harness" / "run_test.py").read_text()
        tree = ast.parse(src)
        rmtree_calls = [n for n in ast.walk(tree)
                        if isinstance(n, ast.Call)
                        and getattr(n.func, "attr", "") == "rmtree"
                        and "_video_tmp" in ast.dump(n)]
        self.assertTrue(rmtree_calls, "no _video_tmp cleanup found")
        # It must not be nested inside the `if src.exists()` success branch.
        for call in rmtree_calls:
            parents = [n for n in ast.walk(tree)
                       if isinstance(n, ast.If) and call in ast.walk(n)
                       and "exists" in ast.dump(n.test)]
            self.assertEqual(parents, [],
                             "_video_tmp cleanup is still on the success path only")


class RecorderConcurrencyTests(TempDataMixin, TestCase):
    def test_status_survives_session_vanishing(self):
        """Two tabs poll status() ~2x/s while another clears the session. The
        code re-read the module global between the None-check and the use."""
        import threading
        from core.services import remote_recorder as rr

        class FakeSession:
            state = "recording"
            code = "x"
            def snapshot(self): return {"state": "recording"}
            def stop(self): pass

        rr._session = FakeSession()
        errors = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                try:
                    rr.status()
                except Exception as exc:      # noqa: BLE001 - that's the point
                    errors.append(exc)

        threads = [threading.Thread(target=poll) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(300):
            rr._session = FakeSession()
            rr._session = None
        stop.set()
        for t in threads:
            t.join(timeout=5)
        rr._session = None
        self.assertEqual(errors, [], f"status() raced: {errors[:3]}")


class ArtifactLayoutTests(TempDataMixin, TestCase):
    def test_no_folder_for_a_run_that_cannot_start(self):
        """A missing test file used to still create an empty results folder --
        one per failed attempt, forever."""
        from core.services.runner import execute_run
        test = Test.objects.create(test_id="M-1", file_path="does-not-exist.py")
        run = Run.objects.create(test=test, status=Run.QUEUED)
        execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.ERROR)
        self.assertIn("missing", run.error_message)
        self.assertFalse((self.cfg.results_dir / run.artifacts_rel).exists())


class HarnessTeardownTests(TestCase):
    """The kill path used to lose everything: interrupting a Playwright sync
    call leaves its driver mid-conversation, so the next call blocks forever.
    Measured before the fix: 67s to settle, no result.json, no timings, no
    video, and a partial recording left in _video_tmp."""

    def setUp(self):
        super().setUp()
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "harness_under_test",
            Path(__file__).parent / "harness" / "run_test.py")
        self.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.h)
        self.h._driver_wedged.clear()
        self.addCleanup(self.h._driver_wedged.clear)

    def test_returns_the_value_when_the_call_is_healthy(self):
        self.assertEqual(self.h._try(5, "ok", lambda: "value"), "value")
        self.assertEqual(self.h._driver_wedged, [])

    def test_a_hung_call_is_abandoned_not_waited_on(self):
        import time as t
        start = t.monotonic()
        self.assertIsNone(self.h._try(1, "hang", lambda: t.sleep(30)))
        self.assertLess(t.monotonic() - start, 5,
                        "a hung teardown step was not time-bounded")
        self.assertEqual(self.h._driver_wedged, ["hang"])

    def test_one_timeout_short_circuits_the_rest(self):
        """Without this, each later step burns its own full budget for
        nothing -- 10+10+30s of a tester staring at a spinner."""
        import time as t
        self.h._try(1, "first", lambda: t.sleep(30))
        ran = []
        start = t.monotonic()
        self.assertIsNone(self.h._try(30, "second", lambda: ran.append(1)))
        self.assertEqual(ran, [], "the body ran despite the short-circuit")
        self.assertLess(t.monotonic() - start, 1)

    def test_ordinary_exceptions_do_not_condemn_the_driver(self):
        """A step can fail for its own reasons (no video configured, say)
        without meaning the browser is unreachable."""
        def boom():
            raise ValueError("no video for this run")
        self.assertIsNone(self.h._try(5, "video path", boom))
        self.assertEqual(self.h._driver_wedged, [])
        self.assertEqual(self.h._try(5, "next", lambda: "still works"),
                         "still works")

    def test_the_alarm_handler_is_always_restored(self):
        """_try borrows SIGALRM, which is also the test-timeout mechanism;
        leaking its handler would break the next timeout."""
        import signal as sg
        original = sg.getsignal(sg.SIGALRM)
        self.h._try(5, "fine", lambda: None)
        self.assertIs(sg.getsignal(sg.SIGALRM), original)
        self.h._try(1, "hangs", lambda: __import__("time").sleep(5))
        self.assertIs(sg.getsignal(sg.SIGALRM), original)
        self.assertEqual(sg.alarm(0), 0, "an alarm was left armed")

    def test_kill_signal_fires_once_so_teardown_can_finish(self):
        """A second SIGTERM (the runner's watchdog sends one ~2s after the
        UI's) used to re-raise inside teardown and abort the video save."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "harness" / "run_test.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "on_term")
        guard = fn.body[0]
        self.assertIsInstance(guard, ast.If,
                              "on_term must guard against repeat signals")
        self.assertIsInstance(guard.body[0], ast.Return)

    def test_result_is_written_before_the_browser_is_touched(self):
        """Status and timings are what testers actually need; they must be on
        disk before any call that could hang."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "harness" / "run_test.py").read_text()
        tree = ast.parse(src)
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
        writes = sorted(c.lineno for c in calls
                        if getattr(c.func, "id", "") == "write_result")
        tries = sorted(c.lineno for c in calls
                       if getattr(c.func, "id", "") == "_try")
        self.assertTrue(tries, "teardown is no longer time-bounded")
        # EVERY bounded browser call must come after a write, not just the
        # first one. An earlier version only checked `any(...)`, which was
        # satisfied by the write_result() in the browser-launch failure path
        # -- so adding a _try before the real verdict write slipped through.
        for line in tries:
            self.assertTrue(
                any(w < line for w in writes),
                f"a call that can hang (line {line}) runs before any "
                f"result.json write ({writes})")
        last_write_before_teardown = max(w for w in writes if w < tries[0])
        self.assertLess(last_write_before_teardown, tries[0])


class ActivityReelSamplingTests(TestCase):
    """A long wait must not be recorded frame-by-frame.

    Content-hash de-duplication handles a blinking caret (a few repeating
    states) but NOT a clock, progress bar or polling table, where every
    repaint genuinely differs. Measured on the demo page's job before this
    logic existed: 51 frames / 2.0 MB for 100 seconds of waiting, which
    extrapolates to ~918 frames and ~35 MB for a 30-minute job.
    """

    def setUp(self):
        super().setUp()
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "harness_reel", Path(__file__).parent / "harness" / "run_test.py")
        self.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.h)
        self.h._frame_flushers.clear()
        self.addCleanup(self.h._frame_flushers.clear)

    def test_idle_interval_is_much_coarser_than_the_active_one(self):
        self.assertGreater(self.h.FRAME_IDLE_INTERVAL,
                           self.h.FRAME_MIN_INTERVAL * 10,
                           "idle sampling is not actually sparse")
        self.assertGreater(self.h.FRAME_IDLE_AFTER, 1,
                           "a pause between two clicks would count as idle")

    def test_note_page_action_marks_the_page_attended(self):
        import time as t
        self.h._last_action[0] = t.time() - 3600
        self.assertGreater(t.time() - self.h._last_action[0],
                           self.h.FRAME_IDLE_AFTER)
        self.h.note_page_action()
        self.assertLess(t.time() - self.h._last_action[0], 1)

    def test_a_long_wait_registers_as_idle_at_both_ends(self):
        """TimedPage stamps activity before AND after a call, so a blocking
        wait counts as idle while it blocks but not once it returns."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "harness" / "run_test.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "timed_call")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "note_page_action"]
        self.assertEqual(len(calls), 2,
                         "expected a stamp on entry and on exit")

    def test_the_held_back_frame_is_flushed_when_the_test_resumes(self):
        """The frame showing the awaited thing paints while the test is still
        blocked, so it looks like one more idle repaint. Dropping it lost the
        completion moment entirely (measured: reel ended 10.7s early)."""
        flushed = []
        self.h._frame_flushers.append(lambda: flushed.append(1))
        self.h.note_page_action()
        self.assertEqual(flushed, [1])

    def test_any_page_use_counts_as_activity_not_just_timed_methods(self):
        """Locator-style code (page.locator(...).click(), get_by_role...)
        never touches an instrumented Page method. Keying activity off the
        timed subset alone made a whole modern test look idle, so its busy
        parts were sampled at the 15s idle rate."""
        class FakePage:
            def locator(self, sel):
                return sel
            def get_by_role(self, role):
                return role
            url = "http://example"

        class FakeCtx:
            def record_timing(self, *a):
                pass

        page = self.h.TimedPage(FakePage(), FakeCtx())
        for use in (lambda: page.locator("#x"),
                    lambda: page.get_by_role("button"),
                    lambda: page.url):
            self.h._last_action[0] = 0.0
            use()
            self.assertGreater(self.h._last_action[0], 0,
                               "this style of page use did not register")

    def test_activity_starts_armed_not_at_the_epoch(self):
        """An unset stamp reads as 'idle since 1970', which would sample the
        very first frames of every run at the idle rate."""
        import time as t
        self.assertLess(t.time() - self.h._last_action[0], 60)

    def test_a_failing_flusher_cannot_break_the_test_run(self):
        def boom():
            raise RuntimeError("disk full")
        self.h._frame_flushers.append(boom)
        self.h.note_page_action()      # must not raise


class PerTestVideoTests(TempDataMixin, TestCase):
    """Video grows with wall-clock time on any repainting page, so a test
    that mostly waits needs to be able to opt out on its own."""

    def test_modes_round_trip_through_the_sidecar(self):
        from core.services.syncer import sidecar_dict, sync_from_files
        (self.tests_dir / "VID-1__quiet.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / "VID-1__quiet.json").write_text(
            json.dumps({"id": "VID-1", "name": "quiet", "video": "off"}))
        sync_from_files()
        test = Test.objects.get(test_id="VID-1")
        self.assertEqual(test.video_mode, "off")
        self.assertEqual(sidecar_dict(test)["video"], "off")

    def test_unset_mode_is_omitted_so_old_sidecars_do_not_churn(self):
        from core.services.syncer import sidecar_dict
        test = Test.objects.create(test_id="VID-2", file_path="v.py")
        self.assertNotIn("video", sidecar_dict(test))

    def test_junk_in_the_sidecar_falls_back_to_inherit(self):
        from core.services.syncer import sync_from_files
        (self.tests_dir / "VID-3__junk.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / "VID-3__junk.json").write_text(
            json.dumps({"id": "VID-3", "name": "junk", "video": "sometimes"}))
        sync_from_files()
        self.assertEqual(Test.objects.get(test_id="VID-3").video_mode, "")

    def test_posted_junk_cannot_reach_the_database(self):
        from core.views import _video_mode
        for bad in ("yes", "TRUE", None, "", "  ", "on;drop", 7):
            self.assertEqual(_video_mode(bad), "",
                             f"{bad!r} should fall back to inherit")
        self.assertEqual(_video_mode(" ON "), "on")
        self.assertEqual(_video_mode("off"), "off")

    def test_the_override_beats_the_global_setting_both_ways(self):
        """Reads the runner's actual decision rather than re-deriving it."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "services" / "runner.py").read_text()
        self.assertIn("test.video_mode == Test.VIDEO_ON", src)
        self.assertIn("test.video_mode == Test.VIDEO_OFF", src)
        # and the flag is what actually gates the argument
        fn = [n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.If)
              and getattr(n.test, "id", "") == "want_video"]
        self.assertTrue(fn, "--video is no longer gated on want_video")


class DemoTargetTests(TempDataMixin, TestCase):
    """The demo page is the hub's own test target: the sample tests and the
    doctor's reachability check both point at it."""

    def test_timer_duration_is_clamped(self):
        import re
        for query, expected in [("", 1800), ("?timer=30", 30), ("?timer=abc", 1800),
                                ("?timer=-5", 1), ("?timer=999999", 7200),
                                ("?timer=", 1800)]:
            body = self.client.get("/demo/" + query).content.decode()
            found = re.search(r'data-remaining="(\d+)"', body)
            self.assertIsNotNone(found, f"no timer on /demo/{query}")
            self.assertEqual(int(found.group(1)), expected, f"for /demo/{query}")

    def test_demo_stays_reachable_behind_the_password_gate(self):
        """With the gate on, the sample tests used to drive the LOGIN page --
        and passed, because a login page also has a title and runs JS."""
        cfg = appconfig.get_config()
        cfg.raw["site"]["password"] = "secret"
        for path in ("/demo/", "/demo/submit/"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200,
                             f"{path} is behind the gate; sample tests break")
        self.assertEqual(self.client.get("/tests/").status_code, 302,
                         "the gate itself must still be closed")

    def test_the_job_timer_markup_the_sample_test_depends_on(self):
        body = self.client.get("/demo/?timer=5").content.decode()
        for hook in ('id="timer-btn"', 'id="timer-panel"',
                     'id="timer-done"', 'id="timer-display"'):
            self.assertIn(hook, body, f"DEMO-006 relies on {hook}")


class VideoModeTransferTests(TempDataMixin, TestCase):
    """The disk workflow is how tests reach the lab: a per-test setting that
    does not survive export/import is worse than not having it."""

    def _make(self, test_id, mode):
        (self.tests_dir / f"{test_id}__t.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / f"{test_id}__t.json").write_text(json.dumps(
            {"id": test_id, "name": "t", "video": mode} if mode
            else {"id": test_id, "name": "t"}))

    def test_export_import_round_trip(self):
        from core.services.backup import export_tests, import_tests
        from core.services.syncer import sync_from_files
        self._make("EX-1", "off")
        self._make("EX-2", "on")
        self._make("EX-3", "")
        sync_from_files()
        blob = export_tests()

        # wipe and bring them back, as a different machine would
        for f in self.tests_dir.glob("EX-*"):
            f.unlink()
        Test.objects.all().delete()
        path = self._tmp / "tests.zip"
        path.write_bytes(blob)
        import_tests(path)
        sync_from_files()

        self.assertEqual(Test.objects.get(test_id="EX-1").video_mode, "off")
        self.assertEqual(Test.objects.get(test_id="EX-2").video_mode, "on")
        self.assertEqual(Test.objects.get(test_id="EX-3").video_mode, "")

    def test_backup_restore_round_trip(self):
        from core.services.backup import build_backup, restore_backup
        from core.services.syncer import sync_from_files
        self._make("BK-1", "off")
        sync_from_files()
        path = self._tmp / "backup.zip"
        path.write_bytes(build_backup())
        (self.tests_dir / "BK-1__t.json").write_text(
            json.dumps({"id": "BK-1", "name": "t"}))     # local drift
        sync_from_files()
        self.assertEqual(Test.objects.get(test_id="BK-1").video_mode, "")
        restore_backup(path, restore_db=False)
        sync_from_files()
        self.assertEqual(Test.objects.get(test_id="BK-1").video_mode, "off")

    def test_the_edit_form_offers_every_mode(self):
        self._make("UI-1", "off")
        from core.services.syncer import sync_from_files
        sync_from_files()
        body = self.client.get("/tests/UI-1/edit/").content.decode()
        self.assertIn('name="video_mode"', body)
        self.assertIn('<option value="off" selected>', body)
        for value in ('value=""', 'value="on"', 'value="off"'):
            self.assertIn(value, body)


class HumanizeLocatorStyleTests(TestCase):
    """The plain-language view exists for testers who do not read Python.
    Locator style is what the Playwright docs teach and what pasted-in code
    looks like, so leaving it as raw code defeats the point of the page."""

    def _lines(self, code):
        from core.services.humanize import humanize_code
        return humanize_code(code)

    def _english(self, statement):
        # humanize_code only reads the run() body, so wrap the snippet.
        out = self._lines(f"def run(page, ctx):\n    {statement}\n")
        self.assertEqual(len(out), 1, out)
        self.assertNotEqual(out[0]["kind"], "code",
                            f"left as raw code: {out[0]['text']}")
        return out[0]["text"]

    def test_locator_actions_read_as_english(self):
        cases = {
            'page.locator("#name").fill("Ada")': "Type “Ada” into #name",
            'page.locator("#go").click()': "Click #go",
            'page.locator("#go").dblclick()': "Double-click #go",
            'page.locator("#agree").check()': "Tick the checkbox #agree",
            'page.locator("#p").select_option("high")': "Choose “high” in #p",
            'page.locator("#q").press("Enter")': "Press Enter in #q",
        }
        for code, expected in cases.items():
            self.assertEqual(self._english(code), expected)

    def test_locator_waits_include_the_limit(self):
        self.assertEqual(
            self._english('page.locator("#r").wait_for(state="visible", timeout=90000)'),
            "Wait until #r appears (up to 1.5 min)")
        self.assertEqual(
            self._english('page.locator("#r").wait_for()'),
            "Wait until #r appears")
        self.assertEqual(
            self._english('page.locator("#s").wait_for(state="hidden")'),
            "Wait until #s is gone")

    def test_get_by_locators_read_as_english(self):
        self.assertEqual(
            self._english('page.get_by_role("button", name="Submit").click()'),
            "Click the button labelled “Submit”")
        self.assertEqual(
            self._english('page.get_by_label("Email").fill("a@b.gov")'),
            "Type “a@b.gov” into the “Email” field")
        self.assertEqual(
            self._english('page.get_by_text("Continue").click()'),
            "Click the text “Continue”")

    def test_long_waits_are_stated_in_readable_units(self):
        from core.services.humanize import _secs
        self.assertEqual(_secs(1800000), "30 min")
        self.assertEqual(_secs(3600000), "1 h")
        self.assertEqual(_secs(5000), "5 s")
        self.assertEqual(_secs("bad"), "?")

    def test_the_shipped_long_wait_sample_is_mostly_english(self):
        """DEMO-006 is what a tester opens to learn the long-wait pattern."""
        from pathlib import Path
        code = (Path(__file__).resolve().parent.parent / "sample_tests"
                / "DEMO-006__long_job_wait.py").read_text()
        lines = self._lines(code)
        english = [l for l in lines if l["kind"] != "code"]
        self.assertGreater(len(english), len(lines) / 2,
                           "most of the long-wait sample reads as raw code")


class DurationDisplayTests(TempDataMixin, TestCase):
    """A 30-minute run is a normal test now; "1802.4s" is not a reading."""

    def test_filter_matches_the_javascript_shape(self):
        from core.templatetags.hubfmt import duration
        self.assertEqual(duration(None), "—")
        self.assertEqual(duration(""), "—")
        self.assertEqual(duration("not a number"), "—")
        self.assertEqual(duration(0), "0s")
        self.assertEqual(duration(42), "42s")
        self.assertEqual(duration(90), "1m 30s")
        self.assertEqual(duration(1802.4), "30m 02s")
        self.assertEqual(duration(3725), "1h 02m")
        self.assertEqual(duration(-3), "0s")

    def test_pages_render_a_long_run_readably(self):
        from django.urls import reverse
        test = Test.objects.create(test_id="LONGRUN-1", file_path="l.py")
        run = Run.objects.create(test=test, status=Run.PASSED,
                                 duration_seconds=1802.4,
                                 finished_at=djtimezone.now())
        for url in (reverse("runs_list"),
                    reverse("run_detail", args=[run.pk]),
                    reverse("test_detail", args=["LONGRUN-1"]),
                    reverse("run_report", args=[run.pk]),
                    reverse("tests_list"),
                    reverse("dashboard"),
                    reverse("metrics")):
            body = self.client.get(url).content.decode()
            self.assertIn("30m 02s", body, f"{url} still shows raw seconds")
            # Every surface, including the summary tiles and hover titles --
            # a 30-minute run should never read as "1802.4s" anywhere.
            for raw in ("1802.4s", "1802.4<small>s</small>", "1802<small>s</small>"):
                self.assertNotIn(raw, body, f"{url} shows raw seconds ({raw})")


class SecretFilePermissionTests(TestCase):
    """These machines are shared. Anyone who can read .secret_key can forge a
    session cookie and walk past the password gate."""

    def _boot_settings(self, data_dir):
        """Re-run settings.py's secret-key block against a temp data dir."""
        import importlib
        import os as _os
        import stat as _stat
        import secrets as _secrets
        secret = data_dir / ".secret_key"
        if secret.exists():
            value = secret.read_text().strip()
            if _stat.S_IMODE(secret.stat().st_mode) & 0o077:
                secret.chmod(0o600)
        else:
            value = _secrets.token_urlsafe(48)
            fd = _os.open(str(secret), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
            with _os.fdopen(fd, "w") as fh:
                fh.write(value)
        return value, secret

    def test_a_new_secret_is_never_world_readable(self):
        import stat as _stat
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        _, secret = self._boot_settings(d)
        mode = _stat.S_IMODE(secret.stat().st_mode)
        self.assertEqual(mode & 0o077, 0, f"secret key is {oct(mode)}")

    def test_a_loose_secret_is_tightened_on_boot(self):
        """A restore, an rsync or a careless cp brings the file back 0644 --
        without this nothing would ever notice."""
        import stat as _stat
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        (d / ".secret_key").write_text("borrowed-key")
        (d / ".secret_key").chmod(0o644)
        value, secret = self._boot_settings(d)
        self.assertEqual(value, "borrowed-key", "the key itself must not change")
        self.assertEqual(_stat.S_IMODE(secret.stat().st_mode) & 0o077, 0,
                         "a world-readable secret key was left as-is")

    def test_settings_does_not_write_then_chmod(self):
        """Writing first and chmod-ing after leaves a readable window."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "testhub" / "settings.py").read_text()
        block = src[src.index("_secret_file ="):src.index("DEBUG =")]
        self.assertIn("os.open", block)
        self.assertIn("0o600", block)
        self.assertNotIn("_secret_file.write_text", block)


class DatabasePermissionTests(TestCase):
    """django_session's session_key IS the login cookie: a world-readable
    db.sqlite3 on a shared machine lets any local user lift a session and
    walk past the password gate. Re-tightened on every boot, like
    .secret_key — a restore or a careless cp loosens it silently."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def _boot(self):
        from types import SimpleNamespace
        from testhub.settings import _harden_data_files
        _harden_data_files(SimpleNamespace(data_dir=self.tmp))

    def test_a_loose_database_is_tightened_on_boot(self):
        import stat as _stat
        for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm"):
            f = self.tmp / name
            f.write_text("x")
            f.chmod(0o644)
        self._boot()
        for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm"):
            mode = _stat.S_IMODE((self.tmp / name).stat().st_mode)
            self.assertEqual(mode & 0o077, 0, f"{name} is {oct(mode)}")

    def test_a_missing_database_is_no_problem(self):
        """First boot runs before migrate has created the file."""
        self._boot()

    def test_a_tight_database_is_left_alone(self):
        import stat as _stat
        db = self.tmp / "db.sqlite3"
        db.write_text("x")
        db.chmod(0o600)
        before = db.stat().st_mtime_ns
        self._boot()
        self.assertEqual(_stat.S_IMODE(db.stat().st_mode), 0o600)
        self.assertEqual(db.stat().st_mtime_ns, before)


class SharedLibraryTests(TempDataMixin, TestCase):
    """Teams arrive with a real code base: page objects, helpers, a package
    of their own. The harness puts the tests folder on sys.path and the
    syncer ignores _-prefixed names, so `from _lib.pages import ...` works
    and _lib is never mistaken for a test. The export/import zip must carry
    the shared code too, or a suite arrives broken on the other machine."""

    def _write_lib_and_test(self, root):
        lib = root / "_lib"
        lib.mkdir(parents=True, exist_ok=True)
        (lib / "__init__.py").write_text("")
        (lib / "helpers.py").write_text("ANSWER = 42\n")
        test = root / "LIB-1__uses_library.py"
        test.write_text("from _lib.helpers import ANSWER\n"
                        "def run(page, ctx):\n"
                        "    return ANSWER\n")
        return test

    def test_harness_loads_a_test_that_imports_shared_code(self):
        import sys
        import tempfile
        from core.harness.run_test import load_test_module
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        self.addCleanup(lambda: sys.modules.pop("_lib", None))
        self.addCleanup(lambda: sys.modules.pop("_lib.helpers", None))
        module = load_test_module(self._write_lib_and_test(root))
        self.assertEqual(module.run(None, None), 42,
                         "the _lib import did not actually execute")

    def test_sync_ignores_the_shared_package(self):
        self._write_lib_and_test(self.cfg.tests_dir)
        summary = sync_from_files()
        self.assertEqual(summary["errors"], [])
        ids = set(Test.objects.values_list("test_id", flat=True))
        self.assertIn("LIB-1", ids)
        self.assertFalse(any("_lib" in i for i in ids))

    def test_export_and_import_carry_the_shared_package(self):
        import tempfile
        from core.services import backup as bk
        self._write_lib_and_test(self.cfg.tests_dir)
        blob = bk.export_tests()
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
        self.assertIn("LIB-1__uses_library.py", names)
        self.assertIn("_lib/helpers.py", names)
        # round-trip into a fresh tests dir
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(other), True)
        zpath = other / "tests.zip"
        zpath.write_bytes(blob)
        for f in list(self.cfg.tests_dir.glob("LIB-1*")):
            f.unlink()
        shutil.rmtree(self.cfg.tests_dir / "_lib")
        report = bk.import_tests(zpath)
        self.assertIn("_lib/helpers.py", report["written"])
        self.assertTrue((self.cfg.tests_dir / "_lib" / "helpers.py").exists())

    def test_import_still_rejects_paths_outside_a_shared_package(self):
        import tempfile
        from core.services import backup as bk
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(other), True)
        zpath = other / "evil.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("sub/dir.py", "x")             # path, not _-prefixed
            zf.writestr("_lib/../../escape.py", "x")   # traversal
            zf.writestr("_lib/.hidden.py", "x")        # hidden
        report = bk.import_tests(zpath)
        self.assertEqual(report["written"], [])
        self.assertEqual(len(report["errors"]), 3)


class SystemClockTests(TestCase):
    """Lab clocks are wrong and no NTP can reach them; the Settings button
    fixes it. The input reaches a system command, so validation is strict."""

    def _set(self, value, euid=0, which=lambda c: f"/usr/bin/{c}"):
        from unittest import mock
        from core.services import systemclock
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(systemclock.os, "geteuid", return_value=euid), \
             mock.patch.object(systemclock.shutil, "which", side_effect=which), \
             mock.patch.object(systemclock, "_run", side_effect=fake_run):
            stamp = systemclock.set_system_time(value)
        return stamp, calls

    def test_sets_via_timedatectl_with_ntp_off_first(self):
        stamp, calls = self._set("2026-08-05T14:30:45")
        self.assertEqual(stamp, "2026-08-05 14:30:45")
        self.assertEqual(calls[0], ["timedatectl", "set-ntp", "false"])
        self.assertEqual(calls[1], ["timedatectl", "set-time", "2026-08-05 14:30:45"])

    def test_garbage_input_is_refused_before_any_command(self):
        from core.services.systemclock import ClockError
        for bad in ("now", "2026-08-05; reboot", "2026-13-40T99:99", ""):
            with self.assertRaises(ClockError, msg=bad):
                self._set(bad)

    def test_non_root_gets_a_sentence_about_the_service(self):
        from core.services.systemclock import ClockError
        with self.assertRaises(ClockError) as caught:
            self._set("2026-08-05T14:30", euid=1000)
        self.assertIn("root", str(caught.exception))

    def test_the_endpoint_rejects_bad_input_cleanly(self):
        from django.urls import reverse
        resp = self.client.post(reverse("api_set_clock"),
                                data='{"datetime": "yesterday"}',
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("date and time", resp.json()["error"])


class AnalyticsTests(TempDataMixin, TestCase):
    """Stats BY WEBSITE VERSION: every run is stamped with target.version,
    and this page pivots history on it. Load iterations must never leak in
    (same class of bug as the v2.6 baseline pollution), and the CSV text
    must carry the test NAME and whether it passed or failed — the two
    things the humans reading a paste actually need."""

    def setUp(self):
        super().setUp()
        self.test = Test.objects.create(test_id="AN-1", name="checkout works",
                                        file_path="AN-1__checkout_works.py")

    def _run(self, version, status, duration, **kw):
        return Run.objects.create(test=self.test, status=status,
                                  target_version=version, trigger="manual",
                                  duration_seconds=duration, **kw)

    def _seed_two_versions(self):
        self._run("1.0", Run.PASSED, 2.0)
        self._run("1.0", Run.PASSED, 2.2)
        self._run("2.0", Run.PASSED, 4.0)
        self._run("2.0", Run.FAILED, 4.4)

    def test_versions_are_ordered_by_first_appearance(self):
        from core.services import analytics as an
        self._seed_two_versions()
        self.assertEqual(an.version_order(), ["1.0", "2.0"])

    def test_summary_counts_and_pass_rates(self):
        from core.services import analytics as an
        self._seed_two_versions()
        by_version = {s["version"]: s for s in an.versions_summary()}
        self.assertEqual(by_version["1.0"]["pass_rate"], 100.0)
        self.assertEqual(by_version["2.0"]["passed"], 1)
        self.assertEqual(by_version["2.0"]["failed"], 1)
        self.assertEqual(by_version["2.0"]["pass_rate"], 50.0)

    def test_trend_names_the_slower_test(self):
        from core.services import analytics as an
        self._seed_two_versions()
        pair, rows = an.speed_trend()
        self.assertEqual(pair, ["1.0", "2.0"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "slower")
        self.assertGreater(rows[0]["delta_s"], 1.5)

    def test_load_iterations_never_reach_analytics(self):
        from core.models import LoadPlan, LoadRun
        from core.services import analytics as an
        self._seed_two_versions()
        plan = LoadPlan.objects.create(test=self.test, name="p",
                                       duration_seconds=60, concurrency=2)
        lr = LoadRun.objects.create(test=self.test, plan=plan, label="p",
                                    duration_seconds=60, concurrency=2,
                                    status=LoadRun.FINISHED)
        for _ in range(50):
            self._run("2.0", Run.PASSED, 0.5, load_run=lr, iteration=1)
        by_version = {s["version"]: s for s in an.versions_summary()}
        self.assertEqual(by_version["2.0"]["runs"], 2,
                         "load iterations leaked into version analytics")

    def test_csv_carries_test_name_and_result_words(self):
        from django.conf import settings as dj
        from core.services import analytics as an
        self._seed_two_versions()
        # a REAL target: the built-in demo reports its own deployed release
        # and ignores target.version (freight ReleaseTests pins that rule)
        self.cfg.raw.setdefault("target", {})["url"] = "https://shop.example.internal/"
        self.cfg.raw["target"]["version"] = "2.0"
        sections = {s["slug"]: s for s in an.csv_sections()}
        current = sections["current"].get("text", "")
        self.assertIn("checkout works", current)
        recent = sections["recent"]["text"]
        self.assertIn("passed", recent)
        self.assertIn("failed", recent)
        self.assertIn("checkout works", recent)
        matrix = sections["matrix"]["text"]
        self.assertIn("1.0", matrix)
        self.assertIn("2.0", matrix)

    def test_the_page_renders_with_the_csv_sections(self):
        from django.urls import reverse
        self._seed_two_versions()
        resp = self.client.get(reverse("analytics"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Copy as CSV")
        self.assertContains(resp, "website_version")


class ActivityStripTests(TempDataMixin, TestCase):
    """The strip in base.html shows what is running and queued on EVERY page,
    with kill one click away — testers should never need to hunt for the
    dashboard to find out why the machine is busy or to stop something."""

    def _mk_run(self, status):
        from django.utils import timezone as tz
        test = Test.objects.create(test_id="STRIP-1", name="strip test",
                                   file_path="STRIP-1__strip_test.py")
        return Run.objects.create(
            test=test, status=status, trigger="manual",
            started_at=tz.now() if status == Run.RUNNING else None)

    def test_the_strip_container_is_on_every_page(self):
        from django.urls import reverse
        for name in ("dashboard", "tests_list", "runs_list", "settings_page"):
            resp = self.client.get(reverse(name))
            self.assertContains(resp, 'id="activity-strip"', count=1,
                                msg_prefix=name)

    def test_status_reports_running_and_queued_with_what_the_strip_needs(self):
        from django.urls import reverse
        self._mk_run(Run.RUNNING)
        queued = Run.objects.create(test=Test.objects.get(test_id="STRIP-1"),
                                    status=Run.QUEUED, trigger="manual")
        payload = self.client.get(reverse("api_status")).json()
        by_status = {r["status"]: r for r in payload["runs"]}
        self.assertIn("running", by_status)
        self.assertIn("queued", by_status)
        for row in payload["runs"]:
            for key in ("id", "test_id", "name", "status", "elapsed", "eta"):
                self.assertIn(key, row)
        self.assertEqual(by_status["queued"]["id"], queued.pk)

    def test_killing_a_queued_run_cancels_it_without_a_runner(self):
        """The strip's ✕ on a queued row must work even though there is no
        process to signal — kill_run finalises still-queued runs directly."""
        from django.urls import reverse
        run = self._mk_run(Run.QUEUED)
        resp = self.client.post(reverse("api_kill_run", args=[run.pk]))
        self.assertEqual(resp.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.KILLED)


class BackupPermissionTests(TempDataMixin, TestCase):
    """A backup zip CONTAINS db.sqlite3, so a world-readable backup would
    undo the 0600 on the database itself — session cookies again."""

    def test_auto_backup_is_not_world_readable(self):
        import stat as _stat
        from core.services import backup as bk
        path = bk.auto_backup(reason="permtest")
        self.addCleanup(path.unlink, True)
        mode = _stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0, f"backup is {oct(mode)}")


class DatabaseDoctorErrorTests(TestCase):
    """`testhub doctor` as a non-root user cannot open the deliberately-0600
    database. That must read as 'private, use sudo' — a FAIL here looks like
    corruption and sends someone hunting a problem that does not exist."""

    def _row(self, exc, euid):
        from core.services.diagnostics import _database_error_check
        return _database_error_check(exc, euid=euid)

    def test_permission_denied_as_non_root_is_a_warn_with_sudo(self):
        import sqlite3 as _sq
        row = self._row(_sq.OperationalError("unable to open database file"),
                        euid=1000)
        self.assertEqual(row["status"], "warn")
        self.assertIn("0600", row["detail"])
        self.assertIn("sudo", row["fix"])

    def test_the_same_error_as_root_stays_a_fail(self):
        """Root can read anything — if root cannot open the db, something is
        genuinely wrong and softening it would hide real corruption."""
        import sqlite3 as _sq
        row = self._row(_sq.OperationalError("unable to open database file"),
                        euid=0)
        self.assertEqual(row["status"], "fail")

    def test_an_unrelated_error_stays_a_fail(self):
        row = self._row(RuntimeError("disk I/O error"), euid=1000)
        self.assertEqual(row["status"], "fail")


class DoctorPermissionCheckTests(TempDataMixin, TestCase):
    """doctor is the field diagnostic — it is what someone runs on a machine
    that is hard to reach. A world-readable secret is invisible until you look."""

    def _perm_check(self):
        from core.services.diagnostics import run_checks
        return next((c for c in run_checks() if c["name"] == "File permissions"), None)

    def test_warns_about_a_readable_secret_key(self):
        secret = self.cfg.data_dir / ".secret_key"
        secret.write_text("k")
        secret.chmod(0o644)
        check = self._perm_check()
        self.assertIsNotNone(check)
        self.assertEqual(check["status"], "warn")
        self.assertIn(".secret_key", check["detail"])
        self.assertIn("chmod 600", check["fix"])

    def test_quiet_when_permissions_are_right(self):
        secret = self.cfg.data_dir / ".secret_key"
        secret.write_text("k")
        secret.chmod(0o600)
        self.assertEqual(self._perm_check()["status"], "ok")

    def test_a_readable_config_only_matters_when_it_holds_a_password(self):
        """config.json is normally fine to read — it is machine settings.
        It only becomes a secret once the shared-password gate is on."""
        secret = self.cfg.data_dir / ".secret_key"
        secret.write_text("k")
        secret.chmod(0o600)
        self.cfg.path.write_text("{}")
        self.cfg.path.chmod(0o644)
        self.assertEqual(self._perm_check()["status"], "ok",
                         "flagged config.json with no password in it")
        self.cfg.raw.setdefault("site", {})["password"] = "labsecret"
        check = self._perm_check()
        self.assertEqual(check["status"], "warn")
        self.assertIn("password", check["detail"])


class DoctorOnAHealthyMachineTests(TempDataMixin, TestCase):
    """`testhub doctor` is its own process: it has no runner in it, and on a
    Mac there is no systemd to hold a sleep lock. Both used to WARN on a
    perfectly healthy machine -- the check must ask the serving hub, and know
    which platform it is on."""

    def _answer(self, payload):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__.return_value = resp
        return resp

    def test_runner_is_judged_by_the_serving_hub(self):
        from core.services.diagnostics import serving_hub_check
        with mock.patch("urllib.request.urlopen",
                        return_value=self._answer({"ok": True, "runner": True})) as url:
            check = serving_hub_check(self.cfg)
        self.assertEqual(check["status"], "ok", check)
        self.assertIn(f":{self.cfg.site_port}/api/health/", url.call_args[0][0])
        self.assertIn("runs tests", check["detail"])

    def test_no_hub_serving_is_said_plainly(self):
        import urllib.error
        from core.services.diagnostics import serving_hub_check
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            check = serving_hub_check(self.cfg)
        self.assertEqual(check["status"], "warn")
        self.assertIn("no Test Hub is serving", check["detail"])
        self.assertIn("testhub serve", check["fix"])

    def test_a_read_only_hub_is_still_flagged(self):
        from core.services.diagnostics import serving_hub_check
        with mock.patch("urllib.request.urlopen",
                        return_value=self._answer({"ok": True, "runner": False})):
            check = serving_hub_check(self.cfg)
        self.assertEqual(check["status"], "warn")
        self.assertIn("read-only", check["detail"])

    def test_doctor_from_the_terminal_reports_the_serving_hub(self):
        from core.services import runner as runner_mod
        from core.services.diagnostics import run_checks
        stub = {"status": "ok", "detail": "", "fix": ""}
        with mock.patch.object(runner_mod, "get", return_value=None), \
                mock.patch("core.services.diagnostics._browser_launch_check",
                           return_value=dict(stub, name="Browser launch")), \
                mock.patch("core.services.diagnostics.target_check",
                           return_value=dict(stub, name="Target reachable")), \
                mock.patch("urllib.request.urlopen",
                           return_value=self._answer({"ok": True, "runner": True})):
            checks = {c["name"]: c for c in run_checks()}
        self.assertEqual(checks["Runner"]["status"], "ok", checks["Runner"])
        from importlib.metadata import version
        self.assertEqual(checks["Playwright library"]["detail"],
                         f"version {version('playwright')}", "no __version__ in Playwright")

    def test_the_serving_process_needs_no_probe(self):
        from core.services import runner as runner_mod
        from core.services.diagnostics import run_checks
        with mock.patch.object(runner_mod, "get", return_value=object()), \
                mock.patch("core.services.diagnostics._browser_launch_check",
                           return_value={"name": "Browser launch", "status": "ok",
                                         "detail": "", "fix": ""}), \
                mock.patch("urllib.request.urlopen") as url:
            checks = {c["name"]: c for c in run_checks()}
        self.assertEqual(checks["Runner"]["status"], "ok")
        self.assertFalse(any("/api/health/" in str(c) for c in url.call_args_list))

    def test_a_mac_is_not_told_to_mask_systemd_targets(self):
        from core.services.diagnostics import sleep_check
        check = sleep_check(self.cfg, platform="darwin")
        self.assertEqual(check["status"], "ok")
        self.assertIn("macOS", check["detail"])
        self.assertNotIn("systemctl", check["detail"] + check["fix"])
        # Linux without a way to take the lock keeps its warning
        with mock.patch("core.services.keepawake.can_inhibit", return_value=False):
            self.assertEqual(sleep_check(self.cfg, platform="linux")["status"], "warn")

    def test_the_playwright_version_is_read_from_its_metadata(self):
        from importlib.metadata import version
        from core.services.diagnostics import _package_version
        self.assertEqual(_package_version("playwright"), version("playwright"))
        self.assertNotEqual(_package_version("playwright"), "unknown")
        self.assertEqual(_package_version("no-such-package-anywhere"), "unknown")


class FapolicydCheckTests(TestCase):
    """fapolicyd (hardened RHEL 8) refuses to execute anything not in its
    allow-list. The lab hit this for real: with the daemon back on, python
    and Playwright died with 'operation not permitted' — looking like a
    broken runner, not a security policy. doctor must say so first."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefix = Path("/opt/pw-offline")
        self.home = Path("/opt/pw-testhub")

    def _etc(self, rules_text=None):
        etc = self.tmp / "fapolicyd"
        (etc / "rules.d").mkdir(parents=True, exist_ok=True)
        if rules_text is not None:
            (etc / "rules.d" / "10-pw-offline.rules").write_text(rules_text)
        return etc

    def _check(self, etc, active=True, browser_dirs=()):
        from unittest import mock
        from core.services.diagnostics import fapolicyd_check
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0 if active else 3)
            return fapolicyd_check(etc=etc, prefix=self.prefix,
                                   app_home=self.home,
                                   browser_dirs=list(browser_dirs))

    def test_no_row_when_fapolicyd_is_not_installed(self):
        """CentOS 7 and dev laptops have no fapolicyd — doctor stays quiet
        rather than warning about a daemon that cannot exist there."""
        self.assertIsNone(self._check(self.tmp / "missing"))

    def test_enforcing_and_allowlisted_is_ok(self):
        etc = self._etc("allow perm=any all : dir=/opt/pw-offline/\n"
                        "allow perm=any all : dir=/opt/pw-testhub/\n")
        check = self._check(etc, active=True)
        self.assertEqual(check["status"], "ok")

    def test_enforcing_without_the_allowlist_warns_with_the_fix(self):
        check = self._check(self._etc(), active=True)
        self.assertEqual(check["status"], "warn")
        self.assertIn("operation not permitted", check["detail"])
        self.assertIn("install-all.sh", check["fix"])

    def test_a_partial_allowlist_names_what_is_missing(self):
        """The base installer knows nothing of the app home; if its dir line
        is gone (hand edit, old file), the warning must name IT, not vaguely
        wave at the whole list."""
        etc = self._etc("allow perm=any all : dir=/opt/pw-offline/\n")
        check = self._check(etc, active=True)
        self.assertEqual(check["status"], "warn")
        self.assertIn("/opt/pw-testhub", check["detail"])
        self.assertNotIn("/opt/pw-offline ", check["detail"])

    def test_an_unlisted_browser_dir_is_named(self):
        """Chrome/Edge/chromium+chromedriver/Firefox arrive as RPMs. The
        rpm trust database should vouch for them, but its cache can lag an
        install done while the daemon was stopped — so the dirs that exist
        on the box must be in the allow-list, and a missing one must be
        named. This is the webdriver case that could not be tested in the
        lab."""
        etc = self._etc("allow perm=any all : dir=/opt/pw-offline/\n"
                        "allow perm=any all : dir=/opt/pw-testhub/\n")
        check = self._check(etc, active=True,
                            browser_dirs=[Path("/usr/lib64/chromium-browser"),
                                          Path("/opt/google/chrome")])
        self.assertEqual(check["status"], "warn")
        self.assertIn("/usr/lib64/chromium-browser", check["detail"])
        self.assertIn("/opt/google/chrome", check["detail"])

    def test_listed_browser_dirs_are_ok(self):
        etc = self._etc("allow perm=any all : dir=/opt/pw-offline/\n"
                        "allow perm=any all : dir=/opt/pw-testhub/\n"
                        "allow perm=any all : dir=/opt/google/chrome/\n")
        check = self._check(etc, active=True,
                            browser_dirs=[Path("/opt/google/chrome")])
        self.assertEqual(check["status"], "ok")

    def test_stopped_daemon_with_rules_staged_suggests_starting_it(self):
        """The install stops fapolicyd and restarts it — but a crash or an
        old installer leaves it off. That box is silently unprotected."""
        etc = self._etc("allow perm=any all : dir=/opt/pw-offline/\n"
                        "allow perm=any all : dir=/opt/pw-testhub/\n")
        check = self._check(etc, active=False)
        self.assertEqual(check["status"], "warn")
        self.assertIn("systemctl start fapolicyd", check["fix"])

    def test_stopped_daemon_without_rules_says_install_first(self):
        """Telling someone to start fapolicyd while the allow-list is missing
        would break every run — the fix must be the installer, not systemctl."""
        check = self._check(self._etc(), active=False)
        self.assertEqual(check["status"], "warn")
        self.assertIn("install-all.sh", check["fix"])
        self.assertNotIn("systemctl start", check["fix"])


class OpencodeCheckTests(TestCase):
    """'opencode: command not found' in the lab was a mistyped env.sh source
    line — which fails silently and takes playwright and robot down with it.
    doctor states where the binary is and the exact line that exposes it."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_no_row_when_no_base_bundle_on_this_machine(self):
        from core.services.diagnostics import opencode_check
        self.assertIsNone(opencode_check(prefix=self.tmp / "missing"))

    def test_installed_binary_is_ok_and_names_the_source_line(self):
        from core.services.diagnostics import opencode_check
        binary = self.tmp / "bin" / "opencode"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        check = opencode_check(prefix=self.tmp)
        self.assertEqual(check["status"], "ok")
        self.assertIn(f"source {self.tmp}/env.sh", check["detail"])

    def test_missing_binary_warns_and_points_at_the_probe(self):
        from core.services.diagnostics import opencode_check
        (self.tmp / "bin").mkdir(parents=True)
        check = opencode_check(prefix=self.tmp)
        self.assertEqual(check["status"], "warn")
        self.assertIn("opencode-probe.txt", check["fix"])


class MalformedTestFileTests(TempDataMixin, TestCase):
    """Testers will put broken files in the tests folder — a half-finished
    edit, a bad paste, a name with an accent in it. Sync must survive the lot
    and keep the good tests working; one bad file must not blank the hub."""

    def _write(self, name, code, sidecar=None):
        (self.tests_dir / name).write_text(code, encoding="utf-8")
        if sidecar is not None:
            (self.tests_dir / name.replace(".py", ".json")).write_text(sidecar)

    def test_sync_survives_a_folder_full_of_bad_files(self):
        self._write("GOOD-1__fine.py", "def run(page, ctx):\n    pass\n")
        self._write("SYNTAX-1__broken.py", "def run(page, ctx)\n    not python\n")
        self._write("NORUN-1__no_run.py", "x = 1\n")
        self._write("EMPTY-1__empty.py", "")
        self._write("UNICODE-1__accents.py",
                    'def run(page, ctx):\n    ctx.log("Ünïcode — ✓ é à ü")\n')
        self._write("BADJSON-1__x.py", "def run(page, ctx):\n    pass\n",
                    sidecar="{not json")

        summary = sync_from_files()          # must not raise
        ids = set(Test.objects.values_list("test_id", flat=True))
        self.assertIn("GOOD-1", ids, "a good test was lost because of a bad one")
        self.assertIn("UNICODE-1", ids, "non-ASCII filename/content broke sync")
        self.assertIn("BADJSON-1", ids,
                      "a corrupt sidecar should fall back to defaults, not drop the test")

    def test_the_ui_still_renders_with_broken_tests_present(self):
        from django.urls import reverse
        self._write("SYNTAX-1__broken.py", "def run(page, ctx)\n    nope\n")
        self._write("UNICODE-1__accents.py",
                    'def run(page, ctx):\n    ctx.log("Ünïcode — ✓")\n')
        sync_from_files()
        for url in (reverse("tests_list"), reverse("dashboard"), reverse("metrics"),
                    reverse("test_detail", args=["SYNTAX-1"]),
                    reverse("test_detail", args=["UNICODE-1"])):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_a_broken_test_fails_its_run_with_a_readable_message(self):
        """Not a traceback wall: the tester needs to know it is their file."""
        from core.services.runner import execute_run
        self._write("SYNTAX-1__broken.py", "def run(page, ctx)\n    nope\n")
        sync_from_files()
        run = Run.objects.create(test=Test.objects.get(test_id="SYNTAX-1"),
                                 status=Run.QUEUED)
        execute_run(run.pk)
        run.refresh_from_db()
        self.assertIn(run.status, (Run.ERROR, Run.FAILED))
        self.assertTrue(run.error_message, "a broken test produced no explanation")

    def test_unicode_survives_the_round_trip_to_disk(self):
        from core.services.syncer import sidecar_dict, write_sidecar
        self._write("UNICODE-1__accents.py",
                    'def run(page, ctx):\n    ctx.log("é à ü ✓")\n')
        sync_from_files()
        test = Test.objects.get(test_id="UNICODE-1")
        test.description = "Contrôle des accents — ✓"
        test.save()
        write_sidecar(test)
        sync_from_files()
        self.assertEqual(Test.objects.get(test_id="UNICODE-1").description,
                         "Contrôle des accents — ✓")
        self.assertIn("✓", json.dumps(sidecar_dict(test), ensure_ascii=False))


class TestFileLoadErrorTests(TempDataMixin, TestCase):
    """A broken test file is the most common thing a tester will produce.
    The message used to be "could not load test file:" and nothing else --
    error_short() strips everything from "Traceback" on, so the only useful
    part was thrown away before the tester ever saw it."""

    def _run(self, name, code):
        from core.services.runner import execute_run
        from core.views import error_short
        (self.tests_dir / name).write_text(code)
        sync_from_files()
        test = Test.objects.get(test_id=name.split("__")[0])
        run = Run.objects.create(test=test, status=Run.QUEUED)
        execute_run(run.pk)
        run.refresh_from_db()
        return run, error_short(run.error_message)

    def test_a_syntax_error_names_the_line_and_the_reason(self):
        run, shown = self._run("SYN-1__x.py", "def run(page, ctx)\n    nope\n")
        self.assertEqual(run.status, Run.ERROR)
        self.assertIn("syntax error", shown.lower())
        self.assertIn("line 1", shown)
        self.assertIn("def run(page, ctx)", shown, "the offending line is not shown")
        self.assertGreater(len(shown.strip()), 30,
                           "the tester was shown a bare header again")

    def test_a_missing_run_function_says_what_is_required(self):
        run, shown = self._run("NOR-1__x.py", "x = 1\n")
        self.assertEqual(run.status, Run.ERROR)
        self.assertIn("run(page, ctx)", shown)
        self.assertNotIn("RuntimeError", shown,
                         "an internal exception name leaked to the tester")

    def test_a_bad_import_names_the_missing_module(self):
        run, shown = self._run("IMP-1__x.py",
                               "import no_such_module_xyz\ndef run(page, ctx):\n    pass\n")
        self.assertEqual(run.status, Run.ERROR)
        self.assertIn("no_such_module_xyz", shown)

    def test_the_full_traceback_is_still_kept_for_whoever_wants_it(self):
        """Readable summary first, but nothing is lost -- the traceback is
        still in the stored message and the run log for a developer."""
        run, shown = self._run("SYN-2__x.py", "def run(page, ctx)\n    nope\n")
        self.assertIn("Traceback", run.error_message)
        self.assertNotIn("Traceback", shown)


class LoadPlanningTests(TempDataMixin, TestCase):
    """The plan is built from a real measurement, not a guess. If the maths
    is wrong, someone commits ten minutes of a lab machine to nothing."""

    def setUp(self):
        super().setUp()
        from core.services import loadtest
        self.lt = loadtest
        self.test = Test.objects.create(test_id="LT-1", file_path="lt.py")

    def _runs(self, *durations):
        for d in durations:
            Run.objects.create(test=self.test, status=Run.PASSED,
                               duration_seconds=d, finished_at=djtimezone.now())

    def test_baseline_uses_the_median_not_the_average(self):
        """One pathological run must not move the plan: an average would be
        dragged by a 60s outlier, a median shrugs it off."""
        self._runs(2.0, 2.1, 1.9, 2.0, 60.0)
        base = self.lt.baseline_for(self.test)
        self.assertEqual(base["source"], "history")
        self.assertEqual(base["samples"], 5)
        self.assertLess(base["seconds"], 3, "an outlier moved the baseline")

    def test_the_baseline_ignores_load_iterations(self):
        """THE bug that makes the whole feature lie.

        Load iterations are measured while the machine is busy, and one
        10-minute run adds ~1300 of them -- so within minutes they are the
        entire recent history. Counting them makes the baseline the LOADED
        time, and the next load test compares loaded against loaded and
        reports 1.0x slowdown. Measured before the fix: a run that was
        genuinely 1.38x slower reported 0.99x.
        """
        from core.models import LoadRun
        self._runs(1.0, 1.0, 1.0)                       # three solo runs
        load_run = LoadRun.objects.create(test=self.test, duration_seconds=60)
        for i in range(50):                             # buried under load ones
            Run.objects.create(test=self.test, load_run=load_run, iteration=i,
                               status=Run.PASSED, duration_seconds=5.0,
                               finished_at=djtimezone.now())
        base = self.lt.baseline_for(self.test)
        self.assertEqual(base["samples"], 3,
                         "load iterations leaked into the baseline")
        self.assertEqual(base["seconds"], 1.0,
                         f"baseline is the loaded time ({base['seconds']}s), "
                         f"so slowdown would read ~1.0x forever")

    def test_a_test_only_ever_load_run_has_no_baseline(self):
        """Better to say "no baseline" than to invent one from loaded data."""
        from core.models import LoadRun
        load_run = LoadRun.objects.create(test=self.test, duration_seconds=60)
        Run.objects.create(test=self.test, load_run=load_run, status=Run.PASSED,
                           duration_seconds=5.0, finished_at=djtimezone.now())
        self.assertIsNone(self.lt.baseline_for(self.test)["seconds"])

    def test_no_history_means_no_projection(self):
        base = self.lt.baseline_for(self.test)
        self.assertIsNone(base["seconds"])
        self.assertEqual(base["source"], "none")
        projection = self.lt.project(None, 600, 4)
        self.assertEqual(projection["iterations"], 0)
        self.assertIn("run the test once", projection["note"])

    def test_projection_scales_with_users_and_window(self):
        self.assertEqual(self.lt.project(2.0, 600, 1)["iterations"], 300)
        self.assertEqual(self.lt.project(2.0, 600, 4)["iterations"], 1200)
        self.assertEqual(self.lt.project(2.0, 60, 4)["iterations"], 120)
        # think time is part of the cycle, not free
        self.assertEqual(self.lt.project(2.0, 600, 1, think_time=2.0)["iterations"], 150)

    def test_a_cap_is_honoured(self):
        self.assertEqual(
            self.lt.project(2.0, 600, 4, max_iterations=50)["iterations"], 50)

    def test_preview_warns_about_the_traps(self):
        self._runs(10.0, 10.0, 10.0)
        # window barely longer than one pass
        short = self.lt.plan_preview(self.test, 12, 2)
        self.assertTrue(any("too short" in w for w in short["warnings"]),
                        short["warnings"])
        # more browsers than the machine can hold
        huge = self.lt.plan_preview(self.test, 600, 10_000)
        self.assertTrue(any("beyond what this machine" in w for w in huge["warnings"]))

    def test_preview_warns_when_there_is_no_baseline(self):
        preview = self.lt.plan_preview(self.test, 600, 4)
        self.assertTrue(any("no completed run" in w for w in preview["warnings"]))

    def test_capacity_is_derived_from_the_real_machine(self):
        """A virtual user is a real Chromium: the ceiling is physical, and
        pretending otherwise measures the machine instead of the app."""
        cap = self.lt.capacity_hint()
        self.assertGreaterEqual(cap["ceiling"], 1)
        self.assertLessEqual(cap["ceiling"], self.lt.HARD_MAX_CONCURRENCY)
        self.assertLessEqual(cap["suggested"], cap["ceiling"])
        self.assertTrue(cap["why"])

    def test_refuses_a_silly_number_of_browsers(self):
        with self.assertRaises(ValueError) as caught:
            self.lt.start_load_run(self.test,
                                   concurrency=self.lt.HARD_MAX_CONCURRENCY + 1)
        self.assertIn("real Chromium", str(caught.exception))


class LoadResultsTests(TempDataMixin, TestCase):
    """The numbers a load run reports have to be the ones people act on."""

    def setUp(self):
        super().setUp()
        from core.models import LoadRun
        from core.services import loadtest
        self.lt = loadtest
        self.test = Test.objects.create(test_id="LR-1", file_path="lr.py")
        self.load_run = LoadRun.objects.create(
            test=self.test, duration_seconds=60, concurrency=2,
            baseline_seconds=1.0, projected_iterations=100,
            started_at=djtimezone.now() - timedelta(seconds=60),
            finished_at=djtimezone.now())

    def _iterations(self, durations, status=Run.PASSED, timings=None):
        import json as _json
        base = self.load_run.started_at
        for i, d in enumerate(durations):
            Run.objects.create(
                test=self.test, load_run=self.load_run, iteration=i + 1,
                status=status, duration_seconds=d,
                started_at=base + timedelta(seconds=i),
                finished_at=base + timedelta(seconds=i + d),
                timings_json=_json.dumps(timings or []))

    def test_percentiles_describe_the_tail_not_the_average(self):
        self._iterations([1.0] * 95 + [9.0] * 5)
        s = self.lt.summarize(self.load_run)
        self.assertEqual(s["completed"], 100)
        self.assertEqual(s["p50"], 1.0)
        self.assertGreaterEqual(s["p95"], 1.0)
        self.assertEqual(s["max"], 9.0)
        self.assertLess(s["avg"], s["max"], "the average hid the tail")

    def test_slowdown_is_measured_against_the_solo_baseline(self):
        self._iterations([2.0] * 10)          # baseline was 1.0
        self.assertEqual(self.lt.summarize(self.load_run)["slowdown_x"], 2.0)

    def test_failures_become_an_error_rate(self):
        self._iterations([1.0] * 8)
        self._iterations([1.0] * 2, status=Run.FAILED)
        s = self.lt.summarize(self.load_run)
        self.assertEqual(s["failed"], 2)
        self.assertEqual(s["error_rate"], 20.0)

    def test_an_empty_run_does_not_explode(self):
        s = self.lt.summarize(self.load_run)
        self.assertEqual(s["completed"], 0)
        self.assertNotIn("p95", s)
        self.assertEqual(self.lt.timeline(self.load_run), [])
        self.assertEqual(self.lt.step_breakdown(self.load_run), [])

    def test_step_breakdown_says_which_step_is_slow(self):
        """The thing a requests-per-second tool cannot tell you."""
        self._iterations([3.0] * 10, timings=[
            {"name": "login", "ms": 100},
            {"name": "dashboard render", "ms": 2500},
        ])
        steps = self.lt.step_breakdown(self.load_run)
        self.assertEqual(steps[0]["name"], "dashboard render",
                         "the slowest step is not listed first")
        self.assertEqual(steps[0]["count"], 10)
        self.assertAlmostEqual(steps[0]["avg"], 2.5, places=2)

    def test_timeline_buckets_show_the_shape_over_time(self):
        self._iterations([1.0] * 30)
        tl = self.lt.timeline(self.load_run, buckets=6)
        self.assertTrue(tl)
        self.assertEqual(sum(b["n"] for b in tl), 30)
        for bucket in tl:
            for key in ("t", "n", "avg", "p95"):
                self.assertIn(key, bucket)


class LoadPlanSidecarTests(TempDataMixin, TestCase):
    """Load plans are DEFINITIONS, so they travel in the test's file. A plan
    designed on the laptop has to survive the disc to the lab."""

    def _write(self, plans):
        (self.tests_dir / "LP-1__t.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / "LP-1__t.json").write_text(json.dumps(
            {"id": "LP-1", "name": "t", "load_plans": plans}))

    def test_plans_arrive_from_the_sidecar(self):
        from core.models import LoadPlan
        self._write([{"name": "Soak", "duration_seconds": 600, "concurrency": 4,
                      "ramp_seconds": 60}])
        sync_from_files()
        plan = LoadPlan.objects.get(name="Soak")
        self.assertEqual(plan.test.test_id, "LP-1")
        self.assertEqual(plan.duration_seconds, 600)
        self.assertEqual(plan.concurrency, 4)
        self.assertEqual(plan.ramp_seconds, 60)

    def test_plans_are_written_back_out(self):
        from core.models import LoadPlan
        from core.services.syncer import sidecar_dict
        self._write([])
        sync_from_files()
        test = Test.objects.get(test_id="LP-1")
        LoadPlan.objects.create(name="Soak", test=test, duration_seconds=300,
                                concurrency=2)
        written = sidecar_dict(test)
        self.assertEqual(len(written["load_plans"]), 1)
        self.assertEqual(written["load_plans"][0]["name"], "Soak")

    def test_a_test_with_no_plans_keeps_a_clean_sidecar(self):
        from core.services.syncer import sidecar_dict
        self._write([])
        sync_from_files()
        self.assertNotIn("load_plans", sidecar_dict(Test.objects.get(test_id="LP-1")))

    def test_removing_a_plan_from_the_file_removes_it(self):
        from core.models import LoadPlan
        self._write([{"name": "Soak", "duration_seconds": 600, "concurrency": 4}])
        sync_from_files()
        self.assertEqual(LoadPlan.objects.count(), 1)
        self._write([])
        sync_from_files()
        self.assertEqual(LoadPlan.objects.count(), 0, "the file is the source of truth")

    def test_editing_a_plan_updates_it_in_place(self):
        """Past load runs stay attached, so history is not orphaned."""
        from core.models import LoadPlan, LoadRun
        self._write([{"name": "Soak", "duration_seconds": 600, "concurrency": 4}])
        sync_from_files()
        plan = LoadPlan.objects.get(name="Soak")
        LoadRun.objects.create(plan=plan, test=plan.test)
        self._write([{"name": "Soak", "duration_seconds": 900, "concurrency": 8}])
        sync_from_files()
        plan.refresh_from_db()
        self.assertEqual(plan.duration_seconds, 900)
        self.assertEqual(plan.concurrency, 8)
        self.assertEqual(LoadRun.objects.filter(plan=plan).count(), 1)


class LoadExecutionTests(TempDataMixin, TestCase):
    """The executor itself: it must not disturb normal runs, must stop when
    asked, and must leave every iteration attributable."""

    def setUp(self):
        super().setUp()
        from core.services import loadtest
        self.lt = loadtest
        (self.tests_dir / "LX-1__t.py").write_text(
            "def run(page, ctx):\n    pass\n")
        sync_from_files()
        self.test = Test.objects.get(test_id="LX-1")

    def test_iterations_are_attributable_and_record_nothing(self):
        """Every pass is a real Run linked back to its load run, numbered, and
        with recording off -- 1300 videos of the same test is not a feature."""
        from core.models import LoadRun
        load_run = LoadRun.objects.create(test=self.test, duration_seconds=5,
                                          concurrency=1)
        run = Run.objects.create(test=self.test, load_run=load_run, iteration=7,
                                 minimal_artifacts=True, status=Run.PASSED)
        self.assertEqual(load_run.iterations.count(), 1)
        self.assertEqual(run.iteration, 7)
        self.assertTrue(run.minimal_artifacts)

    def test_deleting_a_load_run_takes_its_iterations_with_it(self):
        from core.models import LoadRun
        load_run = LoadRun.objects.create(test=self.test, duration_seconds=5)
        for i in range(5):
            Run.objects.create(test=self.test, load_run=load_run, iteration=i)
        self.assertEqual(Run.objects.count(), 5)
        load_run.delete()
        self.assertEqual(Run.objects.count(), 0, "iterations were orphaned")

    def test_killing_only_stops_new_passes(self):
        """In-flight passes are left alone on purpose: cutting them off
        mid-pass poisons the timings the run exists to collect."""
        from core.models import LoadRun
        load_run = LoadRun.objects.create(test=self.test, status=LoadRun.RUNNING,
                                          duration_seconds=600)
        self.assertTrue(self.lt.kill_load_run(load_run.pk))
        load_run.refresh_from_db()
        self.assertTrue(load_run.kill_requested)
        # a second kill on something already stopping is a no-op, not an error
        load_run.status = LoadRun.FINISHED
        load_run.save(update_fields=["status"])
        self.assertFalse(self.lt.kill_load_run(load_run.pk))

    def test_orphaned_load_runs_are_recovered_at_startup(self):
        from core.models import LoadRun
        LoadRun.objects.create(test=self.test, status=LoadRun.RUNNING)
        LoadRun.objects.create(test=self.test, status=LoadRun.QUEUED)
        self.assertEqual(self.lt.recover_orphans(), 2)
        for load_run in LoadRun.objects.all():
            self.assertEqual(load_run.status, LoadRun.ERROR)
            self.assertIn("restarted", load_run.error_message)

    def test_a_full_disk_refuses_before_starting(self):
        """A load test writes many runs; filling the disk corrupts SQLite and
        loses the very results it was run to collect."""
        with mock.patch("shutil.disk_usage") as usage:
            usage.return_value = type("U", (), {"free": 100 * 1e6})()
            with self.assertRaises(ValueError) as caught:
                self.lt.start_load_run(self.test, duration_seconds=10)
        self.assertIn("free", str(caught.exception))


class LoadPagesTests(TempDataMixin, TestCase):
    """The pages have to render before, during and after a run."""

    def setUp(self):
        super().setUp()
        (self.tests_dir / "LPG-1__t.py").write_text("def run(page, ctx):\n    pass\n")
        sync_from_files()
        self.test = Test.objects.get(test_id="LPG-1")

    def test_all_load_pages_render(self):
        from django.urls import reverse
        from core.models import LoadRun
        Run.objects.create(test=self.test, status=Run.PASSED, duration_seconds=2.0,
                           finished_at=djtimezone.now())
        load_run = LoadRun.objects.create(
            test=self.test, duration_seconds=60, concurrency=2,
            baseline_seconds=2.0, started_at=djtimezone.now())
        for i in range(5):
            Run.objects.create(test=self.test, load_run=load_run, iteration=i,
                               status=Run.PASSED, duration_seconds=3.0,
                               started_at=djtimezone.now(),
                               finished_at=djtimezone.now())
        for url in (reverse("load_list"),
                    reverse("load_new"),
                    reverse("load_new") + f"?test={self.test.test_id}",
                    reverse("load_detail", args=[load_run.pk])):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_planner_endpoint_answers_before_anything_starts(self):
        from django.urls import reverse
        Run.objects.create(test=self.test, status=Run.PASSED, duration_seconds=2.0,
                           finished_at=djtimezone.now())
        resp = self.client.post(
            reverse("api_load_preview"),
            data=json.dumps({"test_id": "LPG-1", "duration_seconds": 600,
                             "concurrency": 4}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["baseline"]["seconds"], 2.0)
        self.assertEqual(body["projection"]["iterations"], 1200)
        self.assertIn("ceiling", body["capacity"])

    def test_load_endpoints_survive_malformed_bodies(self):
        from django.urls import reverse
        for url in (reverse("api_load_preview"), reverse("api_load_start")):
            for body in (b"", b"[]", b'"x"', b"{bad"):
                code = self.client.post(url, data=body,
                                        content_type="application/json").status_code
                self.assertLess(code, 500, f"{url} 500'd on {body!r}")


class LoadIterationIsolationTests(TempDataMixin, TestCase):
    """One load run adds ~1300 iterations. If they leak into the ordinary
    views, a single load test buries the test's real history and every number
    a human reads becomes the LOADED number.

    Measured before this fix: 99% of the demo test's runs were load
    iterations, and the ETA shown for a normal run had become 1.38s instead
    of the true 1.02s.
    """

    def setUp(self):
        super().setUp()
        from core.models import LoadRun
        (self.tests_dir / "ISO-1__t.py").write_text("def run(page, ctx):\n    pass\n")
        sync_from_files()
        self.test = Test.objects.get(test_id="ISO-1")
        # one honest run...
        Run.objects.create(test=self.test, status=Run.PASSED, duration_seconds=1.0,
                           queued_at=djtimezone.now(), finished_at=djtimezone.now())
        # ...buried under a load run's worth of slower ones
        self.load_run = LoadRun.objects.create(test=self.test, duration_seconds=60)
        for i in range(200):
            Run.objects.create(test=self.test, load_run=self.load_run, iteration=i,
                               status=Run.PASSED, duration_seconds=5.0,
                               queued_at=djtimezone.now(),
                               finished_at=djtimezone.now())

    def test_run_regular_excludes_them(self):
        self.assertEqual(Run.objects.filter(test=self.test).count(), 201)
        self.assertEqual(Run.regular().filter(test=self.test).count(), 1)

    def test_the_tests_numbers_stay_its_own(self):
        d = stats.test_stats(self.test)
        self.assertEqual(d["total_runs"], 1, "load iterations counted as runs")
        self.assertEqual(d["avg_duration"], 1.0,
                         f"avg is the loaded time ({d['avg_duration']}s)")
        self.assertEqual(d["estimate"], 1.0,
                         "the ETA for a normal run became the loaded duration")

    def test_the_test_page_does_not_list_them(self):
        from django.urls import reverse
        body = self.client.get(
            reverse("test_detail", args=["ISO-1"])).content.decode()
        self.assertEqual(body.count("5.0s"), 0,
                         "load iterations are listed on the test page")

    def test_the_runs_list_hides_them_by_default_but_can_show_them(self):
        from django.urls import reverse
        url = reverse("runs_list")
        default = self.client.get(url).context["page"]
        self.assertEqual(default.paginator.count, 1,
                         "a load test flooded the runs list")
        with_load = self.client.get(url + "?load=1").context["page"]
        self.assertEqual(with_load.paginator.count, 201)

    def test_they_are_still_reachable_from_their_load_run(self):
        """Excluded from the general views, never orphaned."""
        self.assertEqual(self.load_run.iterations.count(), 200)


class LoadThreadHygieneTests(TestCase):
    """Threads that use the ORM own a database connection until they say
    otherwise. Load threads are created per run and then die, so a missing
    close leaks one SQLite handle per virtual user, per run, forever.
    (Measured: handles grew 10 -> 18 -> 26 -> 34 over four runs.)"""

    def test_the_virtual_user_closes_its_connection(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "services" / "loadtest.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_virtual_user")
        self.assertTrue(
            any(isinstance(node, ast.Try) and node.finalbody for node in fn.body),
            "_virtual_user must release its connection in a finally block")
        self.assertIn("close_all", ast.dump(fn))

    def test_the_watcher_closes_too(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "services" / "loadtest.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_await_finish")
        self.assertIn("close_all", ast.dump(fn))


class DashboardLoadRowTests(TempDataMixin, TestCase):
    """A load test is ONE activity. Listing each virtual user separately
    drowns whatever else is running and tells you nothing useful."""

    def setUp(self):
        super().setUp()
        from core.models import LoadRun
        (self.tests_dir / "DL-1__t.py").write_text("def run(page, ctx):\n    pass\n")
        sync_from_files()
        self.test = Test.objects.get(test_id="DL-1")
        self.load_run = LoadRun.objects.create(
            test=self.test, status=LoadRun.RUNNING, concurrency=4,
            duration_seconds=600, projected_iterations=1200,
            started_at=djtimezone.now())
        for i in range(4):
            Run.objects.create(test=self.test, load_run=self.load_run,
                               iteration=i, status=Run.RUNNING,
                               started_at=djtimezone.now())
        Run.objects.create(test=self.test, load_run=self.load_run, iteration=99,
                           status=Run.PASSED, duration_seconds=2.0,
                           finished_at=djtimezone.now())

    def test_iterations_are_not_listed_individually(self):
        from django.urls import reverse
        body = self.client.get(reverse("api_status")).json()
        self.assertEqual(body["runs"], [],
                         "in-flight load iterations flooded the dashboard")

    def test_the_load_run_gets_one_row_with_its_own_progress(self):
        from django.urls import reverse
        body = self.client.get(reverse("api_status")).json()
        self.assertEqual(len(body["load_runs"]), 1)
        row = body["load_runs"][0]
        self.assertEqual(row["test_id"], "DL-1")
        self.assertEqual(row["concurrency"], 4)
        self.assertEqual(row["in_flight"], 4)
        self.assertEqual(row["completed"], 1)
        self.assertEqual(row["projected"], 1200)

    def test_a_normal_run_still_shows_up(self):
        from django.urls import reverse
        Run.objects.create(test=self.test, status=Run.RUNNING,
                           started_at=djtimezone.now())
        body = self.client.get(reverse("api_status")).json()
        self.assertEqual(len(body["runs"]), 1, "a normal run was hidden")
        self.assertEqual(len(body["load_runs"]), 1)

    def test_the_nav_badge_counts_a_load_run_once(self):
        from django.urls import reverse
        ctx = self.client.get(reverse("dashboard")).context
        self.assertEqual(ctx["nav_active_runs"], 1,
                         "the badge counted every virtual user")


class OverlappingLoadRunTests(TempDataMixin, TestCase):
    """Two load tests at once measure each other, not the application."""

    def setUp(self):
        super().setUp()
        from core.services import loadtest
        self.lt = loadtest
        (self.tests_dir / "OV-1__t.py").write_text("def run(page, ctx):\n    pass\n")
        sync_from_files()
        self.test = Test.objects.get(test_id="OV-1")

    def test_a_second_run_that_would_bust_the_ceiling_is_refused(self):
        from core.models import LoadRun
        ceiling = self.lt.capacity_hint()["ceiling"]
        LoadRun.objects.create(test=self.test, status=LoadRun.RUNNING,
                               concurrency=ceiling)
        with self.assertRaises(ValueError) as caught:
            self.lt.start_load_run(self.test, duration_seconds=10, concurrency=2)
        message = str(caught.exception)
        self.assertIn("already running", message)
        self.assertIn("measure each other", message)

    def test_a_finished_run_does_not_block_the_next_one(self):
        from core.models import LoadRun
        LoadRun.objects.create(test=self.test, status=LoadRun.FINISHED,
                               concurrency=self.lt.capacity_hint()["ceiling"])
        # no exception: only ACTIVE runs occupy the machine
        preview = self.lt.plan_preview(self.test, 60, 1)
        self.assertIsNotNone(preview)


class SecurityObserverTests(TestCase):
    """The passive checks. Accuracy matters more than volume here: a false
    finding sends someone chasing a non-problem, and a tool that cries wolf
    gets ignored — which is worse than not having it."""

    def setUp(self):
        super().setUp()
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "harness_security", Path(__file__).parent / "harness" / "security.py")
        self.sec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sec)

    def _observer(self, base_url="https://app.example.gov/", headers=None,
                  cookies=(), external=None, insecure=()):
        obs = self.sec.SecurityObserver(None, base_url)
        obs.main_headers = {k.lower(): v for k, v in (headers or {}).items()}
        obs.main_status = 200
        obs.main_url = base_url
        obs.external_hosts = external or {}
        obs.insecure_requests = set(insecure)
        obs._cookies = lambda: list(cookies)
        return obs

    def _kinds(self, report):
        return {f["kind"] for f in report["findings"]}

    def test_a_well_configured_app_produces_nothing(self):
        """No false positives on a correctly configured site."""
        report = self._observer(headers={
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
            "Strict-Transport-Security": "max-age=63072000",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        }, cookies=[{"name": "sessionid", "httpOnly": True, "secure": True,
                     "sameSite": "Lax"}]).report("")
        self.assertEqual(report["findings"], [],
                         f"false positives: {[f['title'] for f in report['findings']]}")

    def test_missing_headers_are_reported(self):
        report = self._observer(headers={"Server": "nginx"}).report("")
        self.assertIn("missing-header", self._kinds(report))
        titles = " ".join(f["title"] for f in report["findings"])
        self.assertIn("Content-Security-Policy", titles)
        self.assertIn("version-disclosure", self._kinds(report))

    def test_csp_frame_ancestors_counts_as_frame_protection(self):
        """A modern CSP replaces X-Frame-Options; demanding both is noise."""
        report = self._observer(headers={
            "Content-Security-Policy": "frame-ancestors 'none'",
            "Strict-Transport-Security": "max-age=1",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }).report("")
        self.assertNotIn("X-Frame-Options",
                         " ".join(f["title"] for f in report["findings"]))

    def test_external_requests_are_high_on_an_airgapped_network(self):
        report = self._observer(
            external={"cdn.jsdelivr.net": 3, "fonts.googleapis.com": 1}).report("")
        external = [f for f in report["findings"] if f["kind"] == "external-request"]
        self.assertEqual(len(external), 1)
        self.assertEqual(external[0]["severity"], "high")
        self.assertIn("cdn.jsdelivr.net", external[0]["detail"])

    def test_a_csrf_cookie_is_called_out_as_expected_not_as_a_fault(self):
        """It MUST be readable by scripts. Reporting it as a defect sends a
        tester to 'fix' something that would break the application."""
        report = self._observer(cookies=[
            {"name": "csrftoken", "httpOnly": False, "secure": True,
             "sameSite": "Lax"}]).report("")
        cookie_findings = [f for f in report["findings"] if f["kind"] == "cookie-flag"]
        self.assertTrue(cookie_findings)
        self.assertEqual(cookie_findings[0]["severity"], "info")
        self.assertIn("expected", cookie_findings[0]["title"])

    def test_a_session_cookie_without_httponly_is_a_real_finding(self):
        report = self._observer(cookies=[
            {"name": "sessionid", "httpOnly": False, "secure": True,
             "sameSite": "Lax"}]).report("")
        cookie_findings = [f for f in report["findings"] if f["kind"] == "cookie-flag"]
        self.assertEqual(cookie_findings[0]["severity"], "medium")

    def test_plain_http_is_reported_but_not_alarmist(self):
        """An isolated lab may serve HTTP deliberately -- say so."""
        report = self._observer(base_url="http://app.local/").report("")
        finding = next(f for f in report["findings"] if f["kind"] == "no-https")
        self.assertEqual(finding["severity"], "medium")
        self.assertIn("isolated lab", finding["fix"])

    def test_a_leaked_stack_trace_is_high(self):
        report = self._observer().report(
            "<pre>Traceback (most recent call last):\n  File ...</pre>")
        finding = next(f for f in report["findings"] if f["kind"] == "error-disclosure")
        self.assertEqual(finding["severity"], "high")

    def test_ordinary_page_text_is_not_mistaken_for_a_trace(self):
        report = self._observer().report(
            "<p>An error occurred. Please contact support with reference 4821.</p>")
        self.assertNotIn("error-disclosure", self._kinds(report))

    def test_mixed_content_on_https(self):
        report = self._observer(
            insecure={"http://cdn.example.com/a.js"}).report("")
        finding = next(f for f in report["findings"] if f["kind"] == "mixed-content")
        self.assertEqual(finding["severity"], "high")

    def test_every_finding_explains_itself_and_what_to_do(self):
        """These are read by testers, not security engineers."""
        report = self._observer(base_url="http://x/", headers={"Server": "nginx"},
                                external={"a.example": 1}).report("")
        self.assertTrue(report["findings"])
        for finding in report["findings"]:
            for field in ("severity", "kind", "title", "detail"):
                self.assertTrue(finding.get(field), f"{field} empty in {finding}")
            self.assertGreater(len(finding["detail"]), 40,
                               f"detail too thin to act on: {finding['title']}")

    def test_observation_never_raises(self):
        """A security check must never be why a functional test fails."""
        obs = self.sec.SecurityObserver(None, "not a url at all")
        obs._cookies = lambda: [{}]          # a cookie with no fields
        report = obs.report(None)
        self.assertIn("findings", report)
        self.assertIn("counts", report)


class SecurityPostureTests(TempDataMixin, TestCase):
    """Aggregation: current state, grouped by problem."""

    def _run_with(self, test_id, findings, when=None):
        import json as _json
        test, _ = Test.objects.get_or_create(test_id=test_id,
                                             defaults={"file_path": f"{test_id}.py"})
        return Run.objects.create(
            test=test, status=Run.PASSED, duration_seconds=1.0,
            queued_at=when or djtimezone.now(), finished_at=djtimezone.now(),
            security_json=_json.dumps({
                "findings": findings,
                "observed": {"scheme": "http", "external_hosts": {}},
                "counts": {}}))

    def test_the_same_problem_across_tests_is_one_row(self):
        from core.services import security
        finding = {"severity": "medium", "kind": "missing-header",
                   "title": "Missing Content-Security-Policy",
                   "detail": "d", "fix": "f"}
        for test_id in ("A-1", "A-2", "A-3"):
            self._run_with(test_id, [finding])
        posture = security.posture()
        self.assertEqual(len(posture["issues"]), 1,
                         "one problem was listed once per test")
        self.assertEqual(posture["issues"][0]["test_count"], 3)

    def test_only_the_newest_run_of_each_test_counts(self):
        """A finding fixed last week must stop being reported."""
        from core.services import security
        old = djtimezone.now() - timedelta(days=2)
        self._run_with("B-1", [{"severity": "high", "kind": "x",
                                "title": "Old problem", "detail": "d"}], when=old)
        self._run_with("B-1", [], when=djtimezone.now())
        posture = security.posture()
        self.assertEqual(posture["issues"], [], "a fixed finding still reported")

    def test_worst_first(self):
        from core.services import security
        self._run_with("C-1", [
            {"severity": "low", "kind": "a", "title": "Low one", "detail": "d"},
            {"severity": "high", "kind": "b", "title": "High one", "detail": "d"},
            {"severity": "medium", "kind": "c", "title": "Mid one", "detail": "d"}])
        severities = [i["severity"] for i in security.posture()["issues"]]
        self.assertEqual(severities, ["high", "medium", "low"])

    def test_load_iterations_do_not_drive_the_posture(self):
        """Same isolation rule as everywhere else."""
        from core.models import LoadRun
        from core.services import security
        test = Test.objects.create(test_id="D-1", file_path="d.py")
        load_run = LoadRun.objects.create(test=test, duration_seconds=60)
        import json as _json
        Run.objects.create(test=test, load_run=load_run, status=Run.PASSED,
                           duration_seconds=1.0, finished_at=djtimezone.now(),
                           security_json=_json.dumps({"findings": [
                               {"severity": "high", "kind": "z",
                                "title": "From a load pass", "detail": "d"}]}))
        self.assertEqual(security.posture()["issues"], [])

    def test_the_page_renders_in_every_state(self):
        from django.urls import reverse
        self.assertEqual(self.client.get(reverse("security_page")).status_code, 200)
        self._run_with("E-1", [{"severity": "high", "kind": "external-request",
                                "title": "External", "detail": "d", "fix": "f"}])
        body = self.client.get(reverse("security_page")).content.decode()
        self.assertEqual(self.client.get(reverse("security_page")).status_code, 200)
        self.assertIn("not a penetration test", body.lower())


class SecretStoreTests(TempDataMixin, TestCase):
    """Credentials must stay on one machine and never come back out."""

    def setUp(self):
        super().setUp()
        from core import secretstore
        self.store = secretstore

    def test_saved_0600_never_world_readable(self):
        import stat as _stat
        path = self.store.save(self.cfg.data_dir, {"target_password": "hunter2!!"})
        self.assertEqual(_stat.S_IMODE(path.stat().st_mode) & 0o077, 0)

    def test_a_loosened_store_is_tightened(self):
        import stat as _stat
        path = self.store.save(self.cfg.data_dir, {"a": "bbbb"})
        path.chmod(0o644)
        self.assertTrue(self.store.is_loose(path))
        self.assertTrue(self.store.harden(path))
        self.assertEqual(_stat.S_IMODE(path.stat().st_mode) & 0o077, 0)

    def test_round_trip_and_names_only(self):
        self.store.save(self.cfg.data_dir, {"b": "2222", "a": "1111"})
        self.assertEqual(self.store.names(self.cfg.data_dir), ["a", "b"])
        self.assertEqual(self.store.load(self.cfg.data_dir)["a"], "1111")

    def test_a_corrupt_store_is_empty_not_an_exception(self):
        self.store.path_for(self.cfg.data_dir).write_text("{not json")
        self.assertEqual(self.store.load(self.cfg.data_dir), {})

    def test_redactor_removes_values_longest_first(self):
        redact = self.store.redactor(["hunter2!!", "hunter2!!extra"])
        self.assertEqual(redact("pw=hunter2!!extra end"), "pw=*** end")
        self.assertEqual(redact("pw=hunter2!! end"), "pw=*** end")

    def test_redactor_ignores_values_too_short_to_be_safe(self):
        """Replacing every 'ab' would shred the log and hide nothing."""
        redact = self.store.redactor(["ab"])
        self.assertEqual(redact("a table of abbreviations"),
                         "a table of abbreviations")

    def test_credentials_are_not_in_a_backup(self):
        """A backup is carried on a disc and restored elsewhere."""
        from core.services.backup import build_backup
        import io, zipfile
        self.store.save(self.cfg.data_dir, {"target_password": "hunter2!!"})
        names = zipfile.ZipFile(io.BytesIO(build_backup())).namelist()
        self.assertNotIn("secrets.json", names)
        self.assertFalse(any("secret" in n for n in names), names)

    def test_credentials_are_not_in_a_tests_export(self):
        from core.services.backup import export_tests
        import io, zipfile
        self.store.save(self.cfg.data_dir, {"target_password": "hunter2!!"})
        blob = export_tests()
        self.assertNotIn(b"hunter2", blob)
        self.assertFalse(any("secret" in n for n in
                             zipfile.ZipFile(io.BytesIO(blob)).namelist()))


class HarnessRedactionTests(TestCase):
    """Belt and braces on top of 'do not print your password' — because
    people do, and run.log is kept forever and read by whoever opens the run."""

    def setUp(self):
        super().setUp()
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "harness_redact", Path(__file__).parent / "harness" / "run_test.py")
        self.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.h)

    def test_a_missing_secret_fails_loudly(self):
        """Silently returning '' makes a login test fail later, confusingly."""
        ctx = self.h.Ctx("http://x/", self._tmpdir(), "T-1", secrets={})
        with self.assertRaises(KeyError) as caught:
            ctx.secret("target_password")
        self.assertIn("Settings", str(caught.exception))

    def test_a_default_is_honoured(self):
        ctx = self.h.Ctx("http://x/", self._tmpdir(), "T-1", secrets={})
        self.assertEqual(ctx.secret("nope", "fallback"), "fallback")

    def test_secret_is_returned_when_present(self):
        ctx = self.h.Ctx("http://x/", self._tmpdir(), "T-1",
                         secrets={"pw": "hunter2!!"})
        self.assertEqual(ctx.secret("pw"), "hunter2!!")
        self.assertTrue(ctx.has_secret("pw"))

    def test_redaction_covers_stdout_and_result_json(self):
        """Asserts the SOURCE wires both, since the live proof is an
        end-to-end run: a real run of a test that leaked the password four
        different ways produced zero occurrences in run.log, result.json and
        the stored error message."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "harness" / "run_test.py").read_text()
        self.assertIn("sys.stdout = _Filtered", src)
        self.assertIn("sys.stderr = _Filtered", src)
        write_result = src[src.index("def write_result():"):
                           src.index("def on_term(")]
        self.assertIn("_REDACT[0](", write_result,
                      "result.json is written as bytes and bypasses the "
                      "stdout filter — it must be redacted explicitly")

    def _tmpdir(self):
        import shutil, tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        return d


class AuthenticatedCheckTests(TestCase):
    """The checks that only mean something once a test has logged in."""

    def setUp(self):
        super().setUp()
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "harness_sec_auth", Path(__file__).parent / "harness" / "security.py")
        self.sec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sec)

    def _obs(self):
        obs = self.sec.SecurityObserver(None, "https://app.example.gov/")
        obs._cookies = lambda: []
        return obs

    def _kinds(self, obs):
        return {f["kind"] for f in obs.report("")["findings"]}

    def test_a_session_that_does_not_rotate_is_flagged(self):
        obs = self._obs()
        obs.note_login([{"name": "sessionid", "value": "SAME"}],
                       [{"name": "sessionid", "value": "SAME"}])
        self.assertIn("session-fixation", self._kinds(obs))

    def test_a_rotated_session_is_silent(self):
        obs = self._obs()
        obs.note_login([{"name": "sessionid", "value": "BEFORE"}],
                       [{"name": "sessionid", "value": "AFTER"}])
        self.assertNotIn("session-fixation", self._kinds(obs))

    def test_no_pre_login_session_is_not_fixation(self):
        """Nothing was carried over, so there is nothing to report."""
        obs = self._obs()
        obs.note_login([], [{"name": "sessionid", "value": "NEW"}])
        self.assertNotIn("session-fixation", self._kinds(obs))

    def test_a_redirect_for_an_anonymous_caller_is_correct(self):
        """302-to-login is the RIGHT answer. Treating it as a hole was a real
        false positive: Playwright follows redirects by default, so the check
        saw the login page's own 200."""
        obs = self._obs()
        obs.note_protected_response("https://app/private", 302, {}, authenticated=False)
        self.assertNotIn("missing-access-control", self._kinds(obs))
        for status in (401, 403):
            obs = self._obs()
            obs.note_protected_response("https://app/p", status, {}, authenticated=False)
            self.assertNotIn("missing-access-control", self._kinds(obs))

    def test_a_private_page_served_to_anonymous_is_high(self):
        obs = self._obs()
        obs.note_protected_response("https://app/private", 200, {}, authenticated=False)
        findings = [f for f in obs.report("")["findings"]
                    if f["kind"] == "missing-access-control"]
        self.assertEqual(findings[0]["severity"], "high")

    def test_logout_that_leaves_the_session_valid_is_high(self):
        obs = self._obs()
        obs.note_logout(still_valid=True)
        findings = [f for f in obs.report("")["findings"]
                    if f["kind"] == "logout-not-invalidated"]
        self.assertEqual(findings[0]["severity"], "high")

    def test_a_proper_logout_is_silent(self):
        obs = self._obs()
        obs.note_logout(still_valid=False)
        self.assertNotIn("logout-not-invalidated", self._kinds(obs))

    def test_private_pages_should_say_no_store(self):
        obs = self._obs()
        obs.note_protected_response("https://app/p", 200,
                                    {"Cache-Control": "private"}, authenticated=True)
        self.assertIn("cacheable-private-page", self._kinds(obs))
        obs = self._obs()
        obs.note_protected_response("https://app/p", 200,
                                    {"Cache-Control": "no-store"}, authenticated=True)
        self.assertNotIn("cacheable-private-page", self._kinds(obs))

    def test_nothing_is_reported_when_no_login_happened(self):
        """Silence beats guessing about a flow that was never performed.

        (Non-auth findings may still appear -- an observer with no recorded
        document response correctly says so; that is not an auth finding.)"""
        auth_kinds = {"session-fixation", "missing-access-control",
                      "logout-not-invalidated", "cacheable-private-page"}
        self.assertEqual(self._kinds(self._obs()) & auth_kinds, set())


class DemoAuthFlowTests(TempDataMixin, TestCase):
    """The demo target's login exists so the authenticated checks have
    something real to run against — and it must be CORRECT, so the shipped
    example shows a passing result."""

    def test_the_private_page_refuses_anonymous_callers(self):
        from django.urls import reverse
        self.assertEqual(self.client.get(reverse("demo_private")).status_code, 302)

    def test_login_rotates_the_session(self):
        from django.urls import reverse
        self.client.get(reverse("demo_login"))          # creates a session
        before = self.client.cookies.get("sessionid")
        self.client.post(reverse("demo_login"),
                         {"username": "tester", "password": "demo-password"})
        after = self.client.cookies.get("sessionid")
        self.assertTrue(after)
        if before:
            self.assertNotEqual(before.value, after.value,
                                "the demo login must regenerate the session")

    def test_the_weak_mode_deliberately_does_not(self):
        """Kept so the detector can be demonstrated against a real fault."""
        from django.urls import reverse
        url = reverse("demo_login") + "?weak=fixation"
        self.client.get(url)
        before = self.client.cookies.get("sessionid")
        self.client.post(url, {"username": "tester", "password": "demo-password"})
        after = self.client.cookies.get("sessionid")
        if before and after:
            self.assertEqual(before.value, after.value)

    def test_private_pages_are_no_store(self):
        from django.urls import reverse
        self.client.post(reverse("demo_login"),
                         {"username": "tester", "password": "demo-password"})
        response = self.client.get(reverse("demo_private"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_logout_clears_the_session_server_side(self):
        from django.urls import reverse
        self.client.post(reverse("demo_login"),
                         {"username": "tester", "password": "demo-password"})
        self.client.get(reverse("demo_logout"))
        self.assertEqual(self.client.get(reverse("demo_private")).status_code, 302)

    def test_wrong_credentials_are_rejected(self):
        from django.urls import reverse
        response = self.client.post(reverse("demo_login"),
                                    {"username": "tester", "password": "wrong"})
        self.assertEqual(response.status_code, 401)


class KeepAwakeTests(TempDataMixin, TestCase):
    """A machine that suspends at 01:50 misses the 02:00 schedule, and the
    symptom reads as a broken scheduler."""

    def setUp(self):
        super().setUp()
        from core.services import keepawake
        self.ka = keepawake
        self.ka._holders = 0
        self.ka._process = None
        self.addCleanup(setattr, self.ka, "_holders", 0)

    def test_reference_counted_so_parallel_runs_share_one_lock(self):
        starts = []
        with mock.patch.object(self.ka, "_start",
                               side_effect=lambda: starts.append(1) or True):
            self.ka.acquire(); self.ka.acquire(); self.ka.acquire()
        self.assertEqual(len(starts), 1, "took a lock per run instead of one")
        self.assertEqual(self.ka._holders, 3)

    def test_released_only_when_the_last_run_finishes(self):
        with mock.patch.object(self.ka, "_start", return_value=True):
            self.ka.acquire(); self.ka.acquire()
        fake = mock.Mock()
        fake.poll.return_value = None
        self.ka._process = fake
        self.ka.release()
        self.assertEqual(fake.terminate.call_count, 0, "released too early")
        self.ka.release()
        self.assertEqual(fake.terminate.call_count, 1)
        self.assertIsNone(self.ka._process)

    def test_release_without_acquire_is_harmless(self):
        self.ka.release()
        self.assertEqual(self.ka._holders, 0)

    def test_failure_to_lock_never_stops_a_test(self):
        """Worst case is the machine sleeps -- which is where we started."""
        with mock.patch.object(self.ka, "_start", return_value=False):
            self.ka.acquire()          # must not raise
        self.assertEqual(self.ka._holders, 1)
        self.ka.release()

    def test_status_reports_honestly(self):
        state = self.ka.status()
        self.assertIn("held", state)
        self.assertFalse(state["held"])

    def test_the_runner_releases_the_lock_on_every_path(self):
        """There are early returns between acquire and release -- a harness
        that fails to start, a process that will not die. A manual
        acquire/release pair leaked the lock and left the machine awake."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent / "services" / "runner.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "execute_run")
        tries = [n for n in fn.body if isinstance(n, ast.Try) and n.finalbody]
        self.assertTrue(tries, "keepawake is not released in a finally block")
        self.assertIn("release", ast.dump(tries[-1]))


# `caffeinate` ships in the BASE bundle (payload/), which sits beside app/ in
# the main repository. A copy of the hub on its own has no payload/ -- those
# tests have nothing to check there, so they say so instead of failing.
_PAYLOAD = Path(__file__).resolve().parent.parent.parent / "payload"


@skipUnless(_PAYLOAD.is_dir(), "the base bundle's payload/ is not beside this app "
                               "(a standalone copy of the hub)")
class CaffeinateShimTests(TestCase):
    """The shipped `caffeinate`. The command it wraps must ALWAYS run."""

    def _script(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent.parent
                / "payload" / "caffeinate")

    def test_the_shim_ships_and_is_executable(self):
        import os
        script = self._script()
        self.assertTrue(script.exists(), f"{script} is missing")
        self.assertTrue(os.access(script, os.X_OK), "not executable")

    def test_it_is_valid_shell(self):
        import subprocess
        result = subprocess.run(["bash", "-n", str(self._script())],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_the_wrapped_command_runs_even_without_a_lock(self):
        """THE bug this had: with no reachable logind, systemd-inhibit exits
        non-zero and runs nothing -- so `caffeinate -- nightly-job` silently
        did not run the job. Far worse than failing to inhibit."""
        source = self._script().read_text()
        self.assertIn("CAN_INHIBIT", source)
        # the fallback exec of the plain command must exist
        self.assertIn('exec "${ARGS[@]}"', source,
                      "no fallback that runs the command without a lock")

    def test_it_is_installed_by_the_base_installer(self):
        from pathlib import Path
        installer = (Path(__file__).resolve().parent.parent.parent
                     / "payload" / "install.sh").read_text()
        self.assertIn("caffeinate", installer)
        build = (Path(__file__).resolve().parent.parent.parent
                 / "scripts" / "build-bundle.sh").read_text()
        self.assertIn("caffeinate", build, "not staged into the bundle")


class TestTransferCommandTests(TempDataMixin, TestCase):
    """The disc workflow's CLI half. docs/TESTHUB.md promised
    `testhub import` for months and no such command existed -- on a machine
    that is awkward to reach, the CLI is sometimes the only way in."""

    def setUp(self):
        super().setUp()
        (self.tests_dir / "TR-1__one.py").write_text("def run(page, ctx):\n    pass\n")
        (self.tests_dir / "TR-2__two.py").write_text("def run(page, ctx):\n    pass\n")
        sync_from_files()

    def _call(self, name, *args, **kwargs):
        from django.core.management import call_command
        out = io.StringIO()
        call_command(name, *args, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_export_then_import_round_trips(self):
        target = self._tmp / "tests.zip"
        self._call("export_tests", "--out", str(target))
        self.assertTrue(target.exists())

        for path in self.tests_dir.glob("TR-*"):
            path.unlink()
        Test.objects.all().delete()

        self._call("import_tests", str(target))
        self.assertEqual(
            sorted(Test.objects.values_list("test_id", flat=True)),
            ["TR-1", "TR-2"])

    def test_export_can_select_individual_tests(self):
        target = self._tmp / "one.zip"
        self._call("export_tests", "--out", str(target), "--test", "TR-1")
        names = zipfile.ZipFile(target).namelist()
        self.assertTrue(any("TR-1" in n for n in names))
        self.assertFalse(any("TR-2" in n for n in names))

    def test_import_of_a_missing_file_is_a_clean_error(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._call("import_tests", str(self._tmp / "nope.zip"))

    def test_no_overwrite_keeps_what_is_there(self):
        target = self._tmp / "tests.zip"
        self._call("export_tests", "--out", str(target))
        (self.tests_dir / "TR-1__one.py").write_text("# edited locally\n")
        self._call("import_tests", str(target), "--no-overwrite")
        self.assertIn("edited locally",
                      (self.tests_dir / "TR-1__one.py").read_text())

    def test_an_export_never_carries_run_history(self):
        """Importing must not be able to damage the target's records."""
        test = Test.objects.get(test_id="TR-1")
        Run.objects.create(test=test, status=Run.PASSED, duration_seconds=1.0)
        target = self._tmp / "tests.zip"
        self._call("export_tests", "--out", str(target))
        names = zipfile.ZipFile(target).namelist()
        self.assertFalse(any(n.endswith(".sqlite3") for n in names), names)
        self.assertFalse(any("results" in n for n in names), names)


class TsTestSupportTests(TempDataMixin, TestCase):
    """TypeScript tests (.spec.ts) as first-class citizens of the Python
    hub: naming, sync, export, and the adapter's report mapping. The
    adapter's end-to-end behaviour (real node, real browsers) belongs to
    verify-appts-py.sh -- these pin the pure logic."""

    def test_ts_naming_helpers(self):
        from pathlib import Path
        from core.services.syncer import (make_filename_ts, sidecar_path,
                                          test_stem)
        p = Path("PAY-001__refund_flow.spec.ts")
        self.assertEqual(test_stem(p), "PAY-001__refund_flow")
        # NOT .spec.json: sidecars match the convention TS repos already use
        self.assertEqual(sidecar_path(p).name, "PAY-001__refund_flow.json")
        self.assertEqual(sidecar_path(Path("A__b.py")).name, "A__b.json")
        self.assertEqual(make_filename_ts("X-1", "My Test"),
                         "X-1__my_test.spec.ts")

    def test_sync_picks_up_spec_ts_with_type_and_sidecar(self):
        from core.services import syncer
        (self.tests_dir / "TS-1__first_ts.spec.ts").write_text(
            "import { test } from '@playwright/test';\n")
        (self.tests_dir / "TS-1__first_ts.json").write_text(json.dumps({
            "name": "First TS", "timeout_seconds": 42,
            "schedules": [{"days": ["mon"], "time": "02:00"}],
        }))
        (self.tests_dir / "_helper.spec.ts").write_text("// ignored")
        summary = syncer.sync_from_files()
        self.assertIn("TS-1", summary["created"])
        t = Test.objects.get(test_id="TS-1")
        self.assertEqual(t.test_type, Test.TYPE_TS)
        self.assertEqual(t.name, "First TS")
        self.assertEqual(t.timeout_seconds, 42)
        self.assertEqual(t.schedules.count(), 1)
        self.assertFalse(Test.objects.filter(test_id="_helper").exists())
        # python files keep their type
        (self.tests_dir / "PY-1__plain.py").write_text("def run(page, ctx):\n    pass\n")
        syncer.sync_from_files()
        self.assertEqual(Test.objects.get(test_id="PY-1").test_type,
                         Test.TYPE_PYTHON)

    def test_export_carries_spec_ts_and_its_sidecar(self):
        from core.services import backup, syncer
        (self.tests_dir / "TS-2__exported.spec.ts").write_text("// spec")
        (self.tests_dir / "TS-2__exported.json").write_text("{}")
        (self.tests_dir / "_lib").mkdir()
        (self.tests_dir / "_lib" / "pages.ts").write_text("// shared")
        syncer.sync_from_files()
        names = zipfile.ZipFile(io.BytesIO(backup.export_tests())).namelist()
        self.assertIn("TS-2__exported.spec.ts", names)
        self.assertIn("TS-2__exported.json", names)
        self.assertIn("_lib/pages.ts", names)

    def test_map_report_verdicts_and_timings(self):
        from core.harness.run_ts_test import map_report
        passed = {"suites": [{"title": "s.spec.ts", "specs": [
            {"title": "logs in", "tests": [{"results": [
                {"status": "passed", "duration": 812.4,
                 "steps": [{"title": "Before Hooks", "duration": 5},
                           {"title": "fill form", "duration": 300},
                           {"title": "After Hooks", "duration": 4}]}]}]},
        ], "suites": []}]}
        status, error, timings = map_report(passed)
        self.assertEqual(status, "passed")
        self.assertEqual(error, "")
        names = [t["name"] for t in timings]
        self.assertIn("logs in", names)
        self.assertIn("fill form", names)      # a real step -> timing row
        self.assertNotIn("Before Hooks", names)  # fixture hooks are noise

        failing = {"suites": [{"specs": [
            {"title": "ok", "tests": [{"results": [{"status": "passed", "duration": 10}]}]},
            {"title": "boom", "tests": [{"results": [
                {"status": "failed", "duration": 20,
                 "error": {"message": "expect(x).toBe(3)\n    at spec.ts:9"}}]}]},
        ], "suites": []}]}
        status, error, _ = map_report(failing)
        self.assertEqual(status, "failed")
        self.assertIn("1 of 2", error)
        self.assertIn("boom", error)
        self.assertIn("toBe(3)", error)
        self.assertNotIn("at spec.ts", error)   # actionable line, not stack

        status, error, _ = map_report({"suites": []})
        self.assertEqual(status, "failed")
        self.assertIn("no tests", error)

    def test_every_soft_failure_is_listed_with_its_numbers(self):
        # expect.soft() goes on after a failure: the message must name every
        # broken promise (as DEMO-020's does), each with expected/received --
        # and never the code frame or the call log
        from core.harness.run_ts_test import map_report
        frame = "\n\n  48 |     codes.push(last.status());\n> 50 |     expect.soft(codes)"
        report = {"suites": [{"specs": [{"title": "the API keeps its promises", "tests": [
            {"results": [{"status": "failed", "duration": 50, "errors": [
                {"message": "Error: contract: field 'eta' is missing\n\n"
                            "expect(received).toHaveProperty(path)\n\nExpected path: \"eta\""},
                {"message": "\x1b[31mError: limits: 6 in a row answered 200\x1b[39m\n\n"
                            "expect(received).toBe(expected)\n\nExpected: 429\nReceived: 200" + frame},
                {"message": "Error: the reset link worked a SECOND time\n\n"
                            "expect(locator).toHaveCount(expected) failed\n\n"
                            "Locator:  locator('#reset-form')\nExpected: 0\nReceived: 1\n\n"
                            "Call log:\n  - Expected: never shown"},
            ]}]}]}], "suites": []}]}
        status, error, _ = map_report(report)
        self.assertEqual(status, "failed")
        self.assertIn("3 problems:", error)
        self.assertIn("\n  - contract: field 'eta' is missing\n", error)
        self.assertIn("limits: 6 in a row answered 200 (expected 429, received 200)", error)
        self.assertIn("SECOND time (locator('#reset-form'), expected 0, received 1)", error)
        for noise in ("Error: ", "\x1b[", "codes.push", "never shown", "Expected path"):
            self.assertNotIn(noise, error)

    def test_reel_extraction_dedupes_and_names_like_the_python_reel(self):
        import hashlib as _hl
        from core.harness.run_ts_test import extract_reel
        zp = self._tmp / "trace.zip"
        a, b = b"JPEGDATA-A" * 40, b"JPEGDATA-B" * 40
        sha_a, sha_b = (_hl.sha1(a).hexdigest(), _hl.sha1(b).hexdigest())
        events = [
            {"type": "screencast-frame", "sha1": sha_a, "timestamp": 1000.0},
            {"type": "screencast-frame", "sha1": sha_a, "timestamp": 1500.0},
            {"type": "screencast-frame", "sha1": sha_b, "timestamp": 2250.0},
        ]
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("trace.trace",
                        "\n".join(json.dumps(e) for e in events))
            zf.writestr(f"resources/{sha_a}", a)
            zf.writestr(f"resources/{sha_b}", b)
        frames_dir = self._tmp / "frames"
        written, on_clock = extract_reel([zp], frames_dir)
        names = sorted(p.name for p in frames_dir.glob("f-*.jpg"))
        # duplicate content dropped; elapsed-ms zero-padded like run_test.py
        self.assertEqual(written, 2)
        self.assertEqual(names, ["f-0000000000.jpg", "f-0000001250.jpg"])
        # no run clock given: the frames count from the first one, and say so
        self.assertFalse(on_clock)

    # -- 2.21: TypeScript step timings on the replay's clock ---------------

    ZERO = 1790318820357.0             # the report's startTime, epoch ms

    def _trace_zip(self, name="trace.zip", runner=True, frames=()):
        """A trace.zip as @playwright/test 1.58 writes it: the runner's own
        test.trace (steps, monotonic clock, anchored to the wall clock by its
        context-options) and the library trace (calls by stepId, frames)."""
        ev = []
        def step(cid, cat, title, start, end, parent=None):
            b = {"type": "before", "callId": cid, "stepId": cid, "startTime": start,
                 "class": "Test", "method": cat, "title": title, "params": {}}
            if parent:
                b["parentId"] = parent
            ev.append(b)
            if end is not None:
                ev.append({"type": "after", "callId": cid, "endTime": end})
        ev.append({"version": 8, "type": "context-options", "origin": "testRunner",
                   "wallTime": self.ZERO, "monotonicTime": 1000.0})
        step("hook@1", "hook", "Before Hooks", 1003.0, 1900.0)
        step("fixture@2", "fixture", 'Fixture "browser"', 1020.0, 1800.0, "hook@1")
        step("pw:api@3", "pw:api", "Launch browser", 1021.0, 1799.0, "fixture@2")
        step("fixture@12", "fixture", 'Fixture "page"', 1810.0, 1895.0, "hook@1")
        step("pw:api@13", "pw:api", "Create page", 1811.0, 1890.0, "fixture@12")
        step("pw:api@4", "pw:api", 'Navigate to "freight/login/"', 1910.0, 2060.0)
        step("test.step@5", "test.step", "choose a new password", 2100.0, 2400.0)
        step("pw:api@6", "pw:api", 'Fill "Pw-SECRET123" locator(\'#new-password\')',
             2110.0, 2130.0, "test.step@5")
        step("expect@7", "expect", "the reset link worked a SECOND time", 2140.0, 2160.0,
             "test.step@5")
        step("expect@8", "expect", 'Expect "toBe"', 2170.0, 2171.0, "test.step@5")
        step("pw:api@9", "pw:api", 'GET "freight/api/v1/quotes"', 2200.0, 2230.0,
             "test.step@5")
        step("pw:api@10", "pw:api", "Click locator('#never-finished')", 2300.0, None)
        step("hook@11", "hook", "After Hooks", 2410.0, 2500.0)
        lib = [{"version": 8, "type": "context-options", "origin": "library",
                "wallTime": self.ZERO + 900.0, "monotonicTime": 1900.0}]
        for sid, cls, method, params in (
                ("pw:api@13", "BrowserContext", "newPage", {}),
                ("pw:api@4", "Frame", "goto", {"url": "freight/login/"}),
                ("pw:api@6", "Frame", "fill", {"selector": "#new-password",
                                               "value": "Pw-SECRET123"}),
                ("expect@7", "Frame", "expect", {"selector": "#reset-form",
                                                 "expression": "to.have.count"}),
                ("pw:api@9", "APIRequestContext", "fetch",
                 {"url": "freight/api/v1/quotes", "method": "POST",
                  "headers": [{"name": "X-API-Key", "value": "acme-demo-7f3a91c2"}]}),
                ("pw:api@10", "Frame", "click", {"selector": "#never-finished"})):
            lib.append({"type": "before", "callId": "call@" + sid, "stepId": sid,
                        "class": cls, "method": method, "params": params})
        for ts, sha in frames:
            lib.append({"type": "screencast-frame", "sha1": sha, "timestamp": ts})
        zp = self._tmp / name
        with zipfile.ZipFile(zp, "w") as zf:
            if runner:
                zf.writestr("test.trace", "\n".join(json.dumps(e) for e in ev))
            zf.writestr("0-trace.trace", "\n".join(json.dumps(e) for e in lib))
            for _, sha in frames:
                zf.writestr(f"resources/{sha}", sha.encode() * 20)
        return zp

    def _report(self, trace):
        return {"suites": [{"title": "t.spec.ts", "suites": [], "specs": [
            {"title": "resets a password", "tests": [{"results": [
                {"status": "passed", "duration": 700,
                 "startTime": "2026-09-25T06:47:00.357Z",
                 "attachments": [{"name": "trace", "path": str(trace)}],
                 "steps": [{"title": "choose a new password", "duration": 300}]}]}]}]}]}

    def test_trace_timeline_times_the_test_body_and_names_no_values(self):
        from core.harness.run_ts_test import read_trace_timeline
        tl = read_trace_timeline(self._trace_zip())
        self.assertEqual(tl["steps"], [("choose a new password", self.ZERO + 1100.0, 300.0)])
        names = [f"{op} {detail}" for op, detail, _, _ in tl["actions"]]
        self.assertEqual(names, ["goto freight/login/", "fill #new-password",
                                 "toHaveCount #reset-form", "POST freight/api/v1/quotes"])
        blob = json.dumps(tl)
        self.assertNotIn("Pw-SECRET123", blob, "a typed value reached the step names")
        self.assertNotIn("acme-demo-7f3a91c2", blob, "a request header reached the step names")
        self.assertNotIn("SECOND time", blob, "an expect message reads like a failure when it passed")
        self.assertNotIn("Launch browser", blob, "fixture work is not the test's")
        self.assertNotIn("never-finished", blob, "a call a kill cut short has no duration")
        self.assertEqual(tl["actions"][0][2:], (self.ZERO + 910.0, 150.0))
        self.assertEqual(tl["end"], self.ZERO + 1500.0)
        # an engine before the runner trace existed: nothing to say
        self.assertIsNone(read_trace_timeline(self._trace_zip("old.zip", runner=False)))
        self.assertIsNone(read_trace_timeline(self._tmp / "missing.zip"))

    def test_map_report_puts_ts_steps_on_the_replay_clock(self):
        from core.harness.run_ts_test import map_report, read_trace_timeline
        trace = self._trace_zip()
        report = self._report(trace)
        _, _, timings = map_report(report, {str(trace): read_trace_timeline(trace)})
        rows = {t["name"]: t for t in timings}
        self.assertTrue(all("start" in t for t in timings), timings)
        # the test row ends where its trace ends; Playwright's duration
        # (which leaves out the browser launch) is kept as its length
        self.assertEqual((rows["resets a password"]["start"], rows["resets a password"]["t"],
                          rows["resets a password"]["ms"]), (0.8, 1.5, 700.0))
        block = rows["choose a new password"]
        self.assertEqual((block["op"], block["start"], block["t"]), ("block", 1.1, 1.4))
        self.assertEqual((rows["fill #new-password"]["start"], rows["fill #new-password"]["t"]),
                         (1.11, 1.13))
        # no trace to read: blocks keep their durations, with no start to claim
        _, _, bare = map_report(report)
        bare = {t["name"]: t for t in bare}
        self.assertEqual(bare["choose a new password"]["op"], "block")
        self.assertNotIn("start", bare["choose a new password"])
        self.assertEqual(bare["resets a password"]["start"], 0.0)

    def test_reel_frames_share_the_steps_clock(self):
        from core.harness.run_ts_test import extract_reel
        # library clock: wall = ZERO + 900 at monotonic 1900 -> a frame at
        # monotonic 1950 was painted 0.95 s into the test
        trace = self._trace_zip(frames=((1950.0, "a" * 40), (2250.0, "b" * 40)))
        frames_dir = self._tmp / "frames-clock"
        written, on_clock = extract_reel([trace], frames_dir, zero_ms=self.ZERO)
        self.assertTrue(on_clock)
        self.assertEqual(sorted(p.name for p in frames_dir.glob("f-*.jpg")),
                         ["f-0000000950.jpg", "f-0000001250.jpg"])

    def test_pw_report_is_rewritten_redacted(self):
        # @playwright/test's JSON report embeds test stdout/error text and
        # is SERVED from the artifacts dir -- a secret printed by a spec
        # must not survive in it (result.json and the log are already
        # redacted; this closes the third copy).
        import json
        from core.harness.run_ts_test import (_redactor,
                                              load_and_redact_report)
        report = {"suites": [], "stdout": ["the password is hunter22\n"]}
        path = self._tmp / "pw-report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        parsed = load_and_redact_report(path, _redactor(["hunter22"]))
        self.assertEqual(parsed["stdout"], ["the password is hunter22\n"])
        on_disk = path.read_text(encoding="utf-8")
        self.assertNotIn("hunter22", on_disk)
        self.assertIn("********", on_disk)
        # unparseable content: still redacted on disk, mapping returns None
        path.write_text("not json but hunter22 leaked", encoding="utf-8")
        self.assertIsNone(load_and_redact_report(path, _redactor(["hunter22"])))
        self.assertNotIn("hunter22", path.read_text(encoding="utf-8"))


class TerminalLayerTests(TempDataMixin, TestCase):
    """The terminal verbs write INTENT (cli-triggered runs, sidecar edits)
    and never execute anything themselves -- that contract is what makes
    'queue it, log out, it keeps running' true."""

    def _mk(self, test_id="CLI-1", ts=False):
        name = f"{test_id}__thing" + (".spec.ts" if ts else ".py")
        (self.tests_dir / name).write_text(
            "// spec" if ts else "def run(page, ctx):\n    pass\n")
        from core.services import syncer
        syncer.sync_from_files()
        return Test.objects.get(test_id=test_id)

    def test_enqueue_cli_writes_intent_only(self):
        from core.services import terminal
        test = self._mk()
        runs = terminal.enqueue_cli([test])
        self.assertEqual(len(runs), 1)
        run = Run.objects.get(pk=runs[0].pk)
        self.assertEqual(run.status, Run.QUEUED)      # nothing executed here
        self.assertEqual(run.trigger, "cli")          # the adoption marker
        self.assertIsNotNone(run.batch)

    def test_schedule_words_parse(self):
        from core.services.terminal import parse_schedule_words as p
        self.assertEqual(p("mon 02:00")["days"], ["mon"])
        self.assertEqual(p("MON 2:00")["time"], "02:00")   # case + padding forgiven
        self.assertEqual(p("mon 2:00")["time"], "02:00")
        self.assertEqual(len(p("daily 14:30")["days"]), 7)
        self.assertIsNone(p("monday 02:00"))
        self.assertIsNone(p("mon 25:00"))
        self.assertIsNone(p("mon"))

    def test_schedule_lives_in_the_sidecar_and_applies(self):
        from core.services import syncer, terminal
        test = self._mk("CLI-2")
        entry = terminal.add_schedule(test, "fri 03:15")
        self.assertIsNotNone(entry)
        sc = json.loads(syncer.sidecar_path(
            self.tests_dir / test.file_path).read_text())
        self.assertEqual(sc["schedules"][0]["days"], ["fri"])
        self.assertEqual(sc["schedules"][0]["time"], "03:15")
        test.refresh_from_db()
        self.assertEqual(test.schedules.count(), 1)
        terminal.clear_schedules(test)
        self.assertEqual(test.schedules.count(), 0)

    def test_scaffold_both_languages(self):
        from core.services import terminal
        f1 = terminal.scaffold("NEW-P1", "Py Thing")
        f2 = terminal.scaffold("NEW-T1", "Ts Thing", ts=True)
        self.assertTrue(f1.endswith(".py"))
        self.assertTrue(f2.endswith(".spec.ts"))
        self.assertEqual(Test.objects.get(test_id="NEW-P1").test_type,
                         Test.TYPE_PYTHON)
        self.assertEqual(Test.objects.get(test_id="NEW-T1").test_type,
                         Test.TYPE_TS)
        with self.assertRaises(FileExistsError):
            terminal.scaffold("NEW-T1", "Ts Thing", ts=True)

    def test_terminal_commands_smoke(self):
        from io import StringIO
        from django.core.management import call_command
        self._mk("CLI-3")
        out = StringIO()
        call_command("list", stdout=out)
        self.assertIn("CLI-3", out.getvalue())
        out = StringIO()
        call_command("stats", "CLI-3", stdout=out)
        self.assertIn("CLI-3", out.getvalue())
        out = StringIO()
        call_command("status", stdout=out)
        self.assertIn("tests", out.getvalue())
        out = StringIO()
        call_command("schedule", "CLI-3", "daily 09:00", stdout=out)
        self.assertIn("daily 09:00", out.getvalue())
        out = StringIO()
        call_command("schedule", stdout=out)
        self.assertIn("CLI-3", out.getvalue())


class RecorderTsOutputTests(TempDataMixin, TestCase):
    """One recording, two languages: the same semantic steps emit python
    (unchanged) or @playwright/test TypeScript, and apply-to-existing
    follows the target test's language."""

    STEPS = [
        {"action": "click", "selector": "#submit-btn"},
        {"action": "fill", "selector": "#name", "value": "O'Brien \"Q\""},
        {"action": "assert_text", "selector": "#result", "value": "done",
         "timeout_ms": 5000},
        {"action": "wait_visible", "selector": "#late", "timeout_ms": 3000},
    ]

    def test_ts_emitter_maps_every_recorded_action(self):
        from core.services.remote_recorder import build_code_ts
        code = build_code_ts(self.STEPS, "http://target/")
        self.assertIn("import { test, expect } from '@playwright/test';", code)
        self.assertIn("await page.click('#submit-btn');", code)
        # quoting survives apostrophes and double quotes
        self.assertIn("await page.fill('#name', 'O\\'Brien \"Q\"');", code)
        self.assertIn(
            "await expect(page.locator('#result')).toContainText('done', { timeout: 5000 });",
            code)
        self.assertIn(
            "await page.waitForSelector('#late', { state: 'visible', timeout: 3000 });",
            code)
        self.assertIn("await page.goto('./');", code)

    def test_multiline_values_stay_inside_the_string_literal(self):
        # A textarea fill or a "check it says..." on multi-line text puts
        # REAL newlines/tabs in a recorded value. Left raw they split the
        # generated string literal -- the test file would not even parse.
        from core.services.remote_recorder import (build_code,
                                                   build_step_lines_ts)
        steps = [{"action": "fill", "selector": "#msg",
                  "value": "line one\nline two\ttabbed"},
                 {"action": "assert_text", "selector": "#out",
                  "value": "a\r\nb"}]
        py = build_code(steps, "http://target/")
        compile(py, "<generated>", "exec")   # raw newline would SyntaxError
        self.assertIn('"line one\\nline two\\ttabbed"', py)
        for line in build_step_lines_ts(steps):
            self.assertNotIn("\n", line)
            self.assertNotIn("\r", line)
        self.assertIn("'line one\\nline two\\ttabbed'",
                      "".join(build_step_lines_ts(steps)))

    def test_ts_append_adds_a_test_block_and_import_if_missing(self):
        from core.services.remote_recorder import append_steps_to_code_ts
        existing = ("import { test, expect } from '@playwright/test';\n\n"
                    "test('original', async ({ page }) => {\n"
                    "  await page.goto('/');\n});\n")
        out = append_steps_to_code_ts(existing, self.STEPS)
        self.assertIn("test('original'", out)
        self.assertIn("test('recorded addition'", out)
        self.assertEqual(out.count("import { test, expect }"), 1)
        bare = "// no import here\n"
        out2 = append_steps_to_code_ts(bare, self.STEPS)
        self.assertTrue(out2.startswith("import { test, expect }"))

    def test_session_offers_both_languages(self):
        from core.services import remote_recorder as rr
        self.assertIn("def run(page, ctx):", rr.build_code(self.STEPS, "http://t/"))
        self.assertIn("async ({ page })", rr.build_code_ts(self.STEPS, "http://t/"))

    def test_create_test_language_choice(self):
        from core.services.syncer import create_test
        py = create_test("REC-P1", "Recorded Py")
        ts = create_test("REC-T1", "Recorded Ts", language="ts")
        self.assertTrue(py.file_path.endswith(".py"))
        self.assertEqual(py.test_type, Test.TYPE_PYTHON)
        self.assertTrue(ts.file_path.endswith(".spec.ts"))
        self.assertEqual(ts.test_type, Test.TYPE_TS)
        code = (self.tests_dir / ts.file_path).read_text()
        self.assertIn("@playwright/test", code)


class GeneratedFileMustParseTests(TempDataMixin, TestCase):
    """Every file the hub GENERATES must itself parse -- names with
    apostrophes, quotes or newlines are normal and must never produce a
    syntactically broken test."""

    def test_ts_template_survives_an_apostrophe_name(self):
        from core.services.syncer import create_test
        ts = create_test("APO-1", "Bob's \"big\" flow", language="ts")
        code = (self.tests_dir / ts.file_path).read_text()
        self.assertIn("test('Bob\\'s \"big\" flow'", code)

    def test_python_template_survives_hostile_docstring_names(self):
        from core.services.syncer import create_test
        for i, name in enumerate(['ends with a quote"', 'has """ inside',
                                  "trailing backslash\\"]):
            t = create_test(f"DOC-{i}", name)
            src = (self.tests_dir / t.file_path).read_text()
            compile(src, t.file_path, "exec")   # SyntaxError = regression

    def test_scaffold_enforces_the_same_id_rules_as_the_web(self):
        from core.services.syncer import TestFileError
        from core.services import terminal
        for bad in ("../escape", "a/b", "a__b", "", ".hidden"):
            with self.assertRaises(TestFileError, msg=bad):
                terminal.scaffold(bad, "x")
        self.assertEqual(len(list(self.tests_dir.glob("*"))), 0)

    def test_new_command_reports_a_bad_id_as_a_command_error(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("new", "bad/id", "Nope")

    def test_scaffold_ts_quotes_the_name(self):
        from core.services import terminal
        terminal.scaffold("APO-2", "O'Hara", ts=True)
        spec = next(self.tests_dir.glob("APO-2__*.spec.ts")).read_text()
        self.assertIn("test('O\\'Hara'", spec)


class NewTestLanguagePrefillTests(TempDataMixin, TestCase):
    """The New-test form always posts the editor's contents, and the editor
    is prefilled with the PYTHON starter. Choosing TypeScript must never
    save python code into a .spec.ts: client JS swaps a pristine template,
    and the server discards a pristine wrong-language template as the
    backstop tested here."""

    def test_pristine_python_template_plus_ts_choice_writes_ts(self):
        from core.services.syncer import NEW_TEST_TEMPLATE
        from django.urls import reverse
        resp = self.client.post(reverse("test_new"), {
            "test_id": "LANG-1", "name": "Lang check", "language": "ts",
            "code": NEW_TEST_TEMPLATE.format(name="New test"),
            "timeout_seconds": "0", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302)
        spec = next(self.tests_dir.glob("LANG-1__*.spec.ts")).read_text()
        self.assertIn("@playwright/test", spec)
        self.assertNotIn("def run(page, ctx)", spec)

    def test_edited_code_is_never_discarded(self):
        from django.urls import reverse
        resp = self.client.post(reverse("test_new"), {
            "test_id": "LANG-2", "name": "Kept", "language": "ts",
            "code": "import { test } from '@playwright/test';\n"
                    "test('mine', async ({ page }) => { await page.goto('/'); });",
            "timeout_seconds": "0", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302)
        spec = next(self.tests_dir.glob("LANG-2__*.spec.ts")).read_text()
        self.assertIn("test('mine'", spec)

    def test_editor_mode_follows_the_test_language(self):
        from core.services.syncer import create_test
        from django.urls import reverse
        ts = create_test("LANG-3", "Ts test", language="ts")
        resp = self.client.get(reverse("test_edit", args=[ts.test_id]))
        self.assertContains(resp, 'data-mode="javascript"')
        resp = self.client.get(reverse("test_new"))
        self.assertContains(resp, 'data-mode="python"')
        self.assertContains(resp, 'id="tpl-ts"')
        # duplicating a TS test preselects TypeScript for the copy
        resp = self.client.get(reverse("test_new") + f"?copy={ts.test_id}")
        self.assertContains(resp, 'data-mode="javascript"')


class RecorderStartPathTests(TempDataMixin, TestCase):
    """A recording can begin on a page UNDER the target (/recorder/?start=
    freight/ -- the built-in Acme Freight demo), and the generated code must
    replay from that same page. TypeScript navigates with './', never '/':
    baseURL is target.url, and for a target under a path (the demo lives at
    /demo/) '/' is the server root -- the hub's own dashboard."""

    STEPS = [{"action": "click", "selector": "#track-btn"}]

    def test_start_path_stays_on_the_site_under_test(self):
        from core.services.remote_recorder import clean_start_path
        self.assertEqual(clean_start_path("/freight/"), "freight/")
        self.assertEqual(clean_start_path(""), "")
        # leading slashes are stripped: a would-be host becomes a PATH under
        # the target (http://h/demo/evil.example/), never another site
        self.assertEqual(clean_start_path("//evil.example/"), "evil.example/")
        for bad in ("https://evil.example/", "../settings/", "freight//x",
                    "freight/../../x", "a b", "x'y", 'x"y'):
            with self.assertRaises(ValueError, msg=bad):
                clean_start_path(bad)

    def test_generated_code_replays_from_the_start_page(self):
        from core.services.remote_recorder import (append_steps_to_code,
                                                   append_steps_to_code_ts,
                                                   build_code, build_code_ts)
        self.assertIn('page.goto(ctx.base_url + "freight/")',
                      build_code(self.STEPS, "http://h/demo/", "freight/"))
        self.assertIn("page.goto(ctx.base_url)", build_code(self.STEPS, "http://h/demo/"))
        self.assertIn("await page.goto('freight/');",
                      build_code_ts(self.STEPS, "http://h/demo/", "freight/"))
        self.assertIn("await page.goto('./');", build_code_ts(self.STEPS, "http://h/demo/"))
        appended = append_steps_to_code("def run(page, ctx):\n    page.goto(ctx.base_url)\n",
                                        self.STEPS, "freight/")
        self.assertIn('page.goto(ctx.base_url + "freight/")', appended)
        self.assertIn("await page.goto('freight/');",
                      append_steps_to_code_ts("test('x', async () => {});\n",
                                              self.STEPS, "freight/"))

    def test_the_start_api_refuses_to_leave_the_site(self):
        from django.urls import reverse
        resp = self.client.post(reverse("api_remote_rec_start"),
                                data=json.dumps({"start": "https://evil.example/"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_recorder_page_carries_a_valid_start_path_only(self):
        from django.urls import reverse
        self.assertContains(self.client.get(reverse("recorder_page") + "?start=freight/"),
                            'data-start="freight/"')
        self.assertContains(self.client.get(reverse("recorder_page") + "?start=../x"),
                            'data-start=""')


class RunSavedFilesTests(TempDataMixin, TestCase):
    """Files a test saves (downloads, reports) are reachable from the run
    page -- and an HTML or SVG one can never run scripts as the hub."""

    def _run(self, files, **fields):
        test = Test.objects.create(test_id="S-1", file_path="s.py")
        rel = "S-1/run1"
        run = Run.objects.create(test=test, status=Run.PASSED, artifacts_rel=rel,
                                 finished_at=djtimezone.now(), **fields)
        base = self.cfg.results_dir / rel
        for name, body in files.items():
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / name).write_bytes(body)
        return run, rel

    def _listed(self, run):
        import re
        from django.urls import reverse
        page = self.client.get(reverse("run_detail", args=[run.pk])).content.decode()
        return re.findall(r'<tr class="saved-file">.*?target="_blank">([^<]+)</a>', page, re.S)

    def test_only_the_tests_own_files_are_listed(self):
        run, _ = self._run({"run.log": b"x", "result.json": b"{}", "trace.zip": b"z",
                            "security.json": b"{}", "live.jpg": b"j", "video.webm": b"v",
                            "frames/f-100.jpg": b"j", "screenshots/01-a.png": b"p",
                            "test-results/x/trace.zip": b"t", "_video_tmp/a.webm": b"v",
                            "route-plan.csv": b"a,b\n", "reports/a11y-report.html": b"<p>hi</p>"})
        self.assertEqual(self._listed(run), ["reports/a11y-report.html", "route-plan.csv"])

    def test_an_offloaded_run_lists_them_from_its_upload_index(self):
        index = ["run.log", "frames/f-1.jpg", "screenshots/01-a.png", "label.pdf"]
        run, _ = self._run({}, artifacts_remote=True, artifacts_index_json=json.dumps(index))
        self.assertEqual(self._listed(run), ["label.pdf"])

    def test_active_files_are_sandboxed_and_nothing_is_sniffed(self):
        from django.urls import reverse
        run, rel = self._run({"page.html": b"<script>parent.x=1</script>", "map.svg": b"<svg/>",
                              "notes.xhtml": b"<html/>", "data.csv": b"a", "label.pdf": b"%PDF-1.4"})
        for name, sandboxed in (("page.html", True), ("map.svg", True), ("notes.xhtml", True),
                                ("data.csv", False), ("label.pdf", False)):
            resp = self.client.get(reverse("artifact", args=[f"{rel}/{name}"]))
            self.assertEqual(resp.status_code, 200, name)
            self.assertEqual(resp.get("Content-Security-Policy") == "sandbox", sandboxed, name)
            self.assertEqual(resp["X-Content-Type-Options"], "nosniff", name)
            resp.close()


class OffloadedRunLogTests(TempDataMixin, TestCase):
    """With S3 offload (delete_local_after_upload), the run page printed
    "(no log)" for every finished run: it only read the local run.log. It
    now reads the log's tail from the bucket."""

    def _remote_run(self):
        test = Test.objects.create(test_id="L-1", file_path="l.py")
        return Run.objects.create(test=test, status=Run.PASSED, artifacts_rel="L-1/run1",
                                  finished_at=djtimezone.now(), artifacts_remote=True,
                                  artifacts_index_json=json.dumps(["run.log", "result.json"]))

    def _page(self, run):
        from django.urls import reverse
        return self.client.get(reverse("run_detail", args=[run.pk])).content.decode()

    def test_the_log_is_read_from_the_bucket(self):
        run = self._remote_run()
        with mock.patch("core.services.storage.read_tail",
                        return_value="[test] starting L-1\n[harness] result: passed\n") as tail:
            page = self._page(run)
        tail.assert_called_once_with("L-1/run1/run.log")
        self.assertIn("[harness] result: passed", page)
        self.assertNotIn("(no log)", page)

    def test_an_unreadable_bucket_points_at_the_link_not_at_nothing(self):
        run = self._remote_run()
        with mock.patch("core.services.storage.read_tail", return_value=None):
            page = self._page(run)
        self.assertNotIn("(no log)", page)
        self.assertIn("could not be read just now", page)

    def test_read_tail_asks_for_the_tail_only_and_never_raises(self):
        from core.services import storage
        fake = mock.Mock()
        fake.get_object.return_value = {"Body": io.BytesIO(b"...last lines\n")}
        with mock.patch.object(storage, "client", return_value=fake):
            self.assertEqual(storage.read_tail("A-1/run7/run.log"), "...last lines\n")
            kwargs = fake.get_object.call_args.kwargs
            self.assertEqual(kwargs["Range"], "bytes=-20000")
            self.assertTrue(kwargs["Key"].endswith("A-1/run7/run.log"))
            fake.get_object.side_effect = RuntimeError("AccessDenied")
            self.assertIsNone(storage.read_tail("A-1/run7/run.log"))


class RunHistoryTests(TempDataMixin, TestCase):
    """The Groups and Schedules pages list what ran, and each entry opens a
    stats page: result, time, release, every test against its usual."""

    def setUp(self):
        super().setUp()
        from core.services.runner import Runner
        self.runner = Runner()
        self.t1 = Test.objects.create(test_id="H-1", file_path="h1.py")
        self.t2 = Test.objects.create(test_id="H-2", file_path="h2.py")
        self.group = Group.objects.create(name="Nightly")
        self.group.tests.set([self.t1, self.t2])

    def _finish(self, batch, statuses, durations, version="3.0.0"):
        now = djtimezone.now()
        for run, status, secs in zip(batch.runs.order_by("pk"), statuses, durations):
            run.status, run.duration_seconds, run.target_version = status, secs, version
            run.started_at, run.finished_at = now - timedelta(seconds=secs), now
            if status != Run.PASSED:
                run.error_message = ("AssertionError: the total is $23.52, expected $11.76\n"
                                     "Traceback (most recent call last): ...")
            run.save()
        return batch

    def _page(self, name, *args):
        from django.urls import reverse
        return self.client.get(reverse(name, args=args)).content.decode()

    def test_a_fired_schedule_is_linked_to_the_batch_it_ran(self):
        from core.models import Batch
        from core.services.scheduler import SchedulerThread
        sched = Schedule(group=self.group, time_of_day=dtime(9, 0), enabled=True)
        sched.days = [0]
        sched.save()
        SchedulerThread(self.runner).fire(sched, djtimezone.now())
        batch = Batch.objects.get()
        self.assertEqual((batch.trigger, batch.schedule, batch.group), ("schedule", sched, self.group))
        self.assertEqual(batch.runs.count(), 2)
        self.assertIn("Scheduled run: Nightly", self._page("batch_detail", batch.pk))

    def test_the_groups_page_lists_every_group_run_with_its_result(self):
        from django.urls import reverse
        by_button = self._finish(self.runner.enqueue_tests(
            [self.t1, self.t2], trigger="group", group=self.group), [Run.PASSED, Run.FAILED], [2, 3])
        not_a_group = self.runner.enqueue_tests([self.t1], trigger="manual")
        history = self._page("groups_list").split('id="group-history"', 1)[1]
        self.assertIn(f'data-batch="{by_button.pk}"', history)
        self.assertNotIn(f'data-batch="{not_a_group.pk}"', history)
        for expected in ("1/2 passed", "1 failed", "Run group button", "3.0.0",
                         reverse("batch_detail", args=[by_button.pk])):
            self.assertIn(expected, history)

    def test_the_schedules_page_shows_what_each_schedule_ran(self):
        from django.urls import reverse
        sched = Schedule(test=self.t1, time_of_day=dtime(7, 0), enabled=False)
        sched.days = [0, 1]
        sched.save()
        linked = self._finish(self.runner.enqueue_tests(
            [self.t1], trigger="schedule", label="schedule: H-1 07:00", schedule=sched),
            [Run.PASSED], [1.5])
        before_216 = self.runner.enqueue_tests([self.t2], trigger="schedule",
                                               label="schedule: H-2 06:00")
        table, history = self._page("schedules_list").split('id="schedule-history"', 1)
        self.assertIn(f'data-batch="{linked.pk}"', history)
        self.assertIn(f'data-batch="{before_216.pk}"', history)
        self.assertIn("schedule: H-2 06:00", history, "an unlinked older run shows its label")
        self.assertIn("paused", history)
        self.assertIn(reverse("batch_detail", args=[linked.pk]), table, "the Last run column")
        self.assertIn("1/1 passed", table)

    def test_the_stats_page_compares_each_test_with_its_usual(self):
        from core.models import LoadRun
        for _ in range(5):          # H-1 usually takes 2 s
            self._finish(self.runner.enqueue_tests([self.t1]), [Run.PASSED], [2.0])
        load = LoadRun.objects.create(test=self.t1, duration_seconds=60)
        for _ in range(30):         # a load test's slow iterations must never count
            Run.objects.create(test=self.t1, load_run=load, status=Run.PASSED,
                               duration_seconds=40.0, finished_at=djtimezone.now())
        batch = self._finish(self.runner.enqueue_tests(
            [self.t1, self.t2], trigger="group", group=self.group),
            [Run.PASSED, Run.FAILED], [3.0, 1.0], version="2.1.0")
        page = self._page("batch_detail", batch.pk)
        self.assertIn("Group run: Nightly", page)
        self.assertIn("50%", page)                               # pass rate
        self.assertIn("2.1.0", page)                             # release
        self.assertIn("50% slower", page, "3 s against a usual 2 s")
        self.assertNotIn("faster", page.split('id="batch-runs"', 1)[1].split("</tbody>")[0],
                         "load iterations (40 s) leaked into the usual")
        self.assertIn("the total is $23.52, expected $11.76", page)
        self.assertNotIn("Traceback", page.split('id="batch-runs"', 1)[1])
        older = self._finish(self.runner.enqueue_tests(
            [self.t1], trigger="group", group=self.group), [Run.PASSED], [2.0])
        self.assertIn('id="batch-newer"', self._page("batch_detail", batch.pk))
        self.assertIn('id="batch-older"', self._page("batch_detail", older.pk))

    def test_terminal_batches_say_so_old_and_new(self):
        from core.models import Batch
        from core.services import terminal
        runs = terminal.enqueue_cli([self.t1])
        self.assertEqual(runs[0].batch.trigger, "terminal")
        old = Batch.objects.create(label="2 tests (terminal)", trigger="manual")
        Run.objects.create(test=self.t1, batch=old, trigger="cli")
        self.assertEqual(stats.batch_summary(old)["source"], "Terminal")
        self.assertEqual(stats.batch_summary(runs[0].batch)["source"], "Terminal")


class ServeMigrationBackupTests(TempDataMixin, TestCase):
    """`serve` migrates the database at start -- on the everything-image, the
    LIVE one in a volume install-app.sh never touches. It must copy it first
    when (and only when) a migration is pending."""

    def test_no_backup_when_nothing_is_pending(self):
        from core.services.backup import backup_before_migrating
        self.assertIsNone(backup_before_migrating())
        self.assertFalse((self.cfg.data_dir / "backups").exists())

    def test_a_pending_migration_gets_a_private_backup_first(self):
        import sqlite3
        import stat
        from django.db.migrations.executor import MigrationExecutor
        from core.services.backup import backup_before_migrating
        db = self.cfg.data_dir / "db.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("create table t (x)")
        con.execute("insert into t values ('history')")
        con.commit()
        con.close()
        with mock.patch.object(MigrationExecutor, "migration_plan",
                               return_value=[("core.0010_future", False)]):
            path = backup_before_migrating()
        self.assertIsNotNone(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with zipfile.ZipFile(path) as zf:
            self.assertIn("db.sqlite3", zf.namelist())
            copy = self.cfg.data_dir / "restored.sqlite3"
            copy.write_bytes(zf.read("db.sqlite3"))
        self.assertEqual(sqlite3.connect(str(copy)).execute("select x from t").fetchone(), ("history",))


class RunPageReplayTests(TempDataMixin, TestCase):
    """2.17: no continuous video by default or on the page; the step timings
    read on the replay's clock (start times, same format), follow it, and
    have no duration bar."""

    TIMINGS = [   # as the harness records them: at completion, t = END
        {"name": "goto /track/", "op": "goto", "detail": "", "ms": 239.0, "t": 0.96},
        {"name": "fill #track-id", "op": "fill", "detail": "", "ms": 85.0, "t": 1.59},
        {"name": "track a shipment", "op": "block", "detail": "", "ms": 1150.0, "t": 1.85},
    ]

    def _run(self, test_type="python", video=True, frames=True):
        test = Test.objects.create(test_id="R-1", file_path="r.py", test_type=test_type)
        run = Run.objects.create(test=test, status=Run.PASSED, artifacts_rel="R-1/run1",
                                 finished_at=djtimezone.now(), duration_seconds=2.0,
                                 timings_json=json.dumps(self.TIMINGS))
        base = self.cfg.results_dir / "R-1/run1"
        (base / "frames").mkdir(parents=True)
        if frames:
            for ms in (1077, 1650, 2098):
                (base / "frames" / f"f-{ms:010d}.jpg").write_bytes(b"jpg")
        (base / "run.log").write_text("log")
        if video:
            (base / "video.webm").write_bytes(b"webm")
        return run

    def _page(self, run):
        from django.urls import reverse
        return self.client.get(reverse("run_detail", args=[run.pk])).content.decode()

    def test_steps_are_listed_by_start_on_the_replay_clock(self):
        import re
        page = self._page(self._run())
        rows = re.findall(r'<tr data-start="([\d.]+)" data-end="([\d.]+)"', page)
        starts = [float(s) for s, _ in rows]
        self.assertEqual(starts, sorted(starts), "not in start order")
        self.assertEqual(starts[0], 0.7, "the block (0.70-1.85) comes before the steps inside it")
        self.assertIn("+0.72s", page)                   # goto started 0.96 - 0.239
        self.assertIn('class="data steps synced"', page)
        self.assertIn("239 ms", page)
        self.assertNotIn("tbar", page, "the duration bar is gone")

    def test_typescript_steps_without_start_times_are_listed_but_not_synced(self):
        # a TS run from before 2.21 (or one that kept no trace)
        page = self._page(self._run(test_type="ts"))
        self.assertIn('class="data steps"', page)
        self.assertNotIn("steps synced", page)
        self.assertIn("kept no step start times", page)
        self.assertIn("test.step(", page, "a TypeScript run shows the TypeScript way to add a phase")

    def test_typescript_steps_with_trace_start_times_follow_the_replay(self):
        import re
        run = self._run(test_type="ts")
        run.timings_json = json.dumps([
            {"name": "resets a password", "op": "test", "detail": "", "ms": 1500.0,
             "t": 2.2, "start": 0.7},
            {"name": "choose a new password", "op": "block", "detail": "", "ms": 900.0,
             "t": 2.0, "start": 1.1},
            {"name": "fill #new-password", "op": "fill", "detail": "", "ms": 20.0,
             "t": 1.13, "start": 1.107},
        ])
        run.save()
        page = self._page(run)
        self.assertIn('class="data steps synced"', page)
        self.assertNotIn("kept no step start times", page)
        starts = [float(x) for x in re.findall(r'<tr data-start="([\d.]+)"', page)]
        self.assertEqual(starts, [0.7, 1.1, 1.107], "the recorded starts, not t - ms")
        # the test case and the test.step() are phases around the action
        self.assertEqual(page.count('data-block="1"'), 2)

    def test_no_video_player_but_a_recorded_video_keeps_its_link(self):
        from django.urls import reverse
        page = self._page(self._run(video=True))
        self.assertNotIn("<video", page)
        self.assertNotIn("Continuous video", page)
        self.assertIn(reverse("artifact", args=["R-1/run1/video.webm"]), page)

    def test_video_is_off_unless_asked_for(self):
        self.cfg.raw["runner"].pop("video", None)
        self.assertFalse(self.cfg.video)
        example = json.loads((appconfig.APP_DIR / "config.example.json").read_text())
        self.assertFalse(example["runner"]["video"], "fresh installs copy the example config")

    def test_the_replay_clock_format(self):
        from core.templatetags.hubfmt import at, ms_short
        self.assertEqual([at(0.72), at(62.5), at(3723.5), at(None)],
                         ["+0.72s", "+1:02.50", "+1:02:03.50", "—"])
        self.assertEqual([ms_short(239), ms_short(1440), ms_short(185000)],
                         ["239 ms", "1.44 s", "3m 05s"])


class PurgeVideosTests(TempDataMixin, TestCase):
    """testhub purge_videos: reports by default; --delete removes video.webm
    (disk and bucket) and nothing else."""

    def setUp(self):
        super().setUp()
        test = Test.objects.create(test_id="V-1", file_path="v.py")
        self.local = Run.objects.create(test=test, status=Run.PASSED, artifacts_rel="V-1/local")
        base = self.cfg.results_dir / "V-1/local"
        (base / "frames").mkdir(parents=True)
        (base / "video.webm").write_bytes(b"w" * 3000)
        (base / "frames" / "f-0000000100.jpg").write_bytes(b"j")
        (base / "run.log").write_text("log")
        self.remote = Run.objects.create(
            test=test, status=Run.PASSED, artifacts_rel="V-1/remote", artifacts_remote=True,
            artifacts_index_json=json.dumps(["run.log", "video.webm", "frames/f-1.jpg"]))

    def test_report_then_delete_only_the_videos(self):
        from django.core.management import call_command
        out = io.StringIO()
        with mock.patch("core.services.storage.object_size", return_value=5_000_000), \
                mock.patch("core.services.storage.delete_object", return_value=True) as delete:
            call_command("purge_videos", stdout=out)
            self.assertIn("2 run(s) have a video", out.getvalue())
            self.assertTrue((self.cfg.results_dir / "V-1/local/video.webm").exists(), "a report deleted")
            delete.assert_not_called()
            out = io.StringIO()
            call_command("purge_videos", "--delete", stdout=out)
        self.assertIn("removed the video of 2 run(s)", out.getvalue())
        base = self.cfg.results_dir / "V-1/local"
        self.assertFalse((base / "video.webm").exists())
        self.assertTrue((base / "frames" / "f-0000000100.jpg").exists(), "frames must stay")
        self.assertTrue((base / "run.log").exists(), "the log must stay")
        delete.assert_called_once_with("V-1/remote/video.webm")
        self.remote.refresh_from_db()
        self.assertEqual(json.loads(self.remote.artifacts_index_json), ["run.log", "frames/f-1.jpg"])

    def test_a_failed_bucket_delete_keeps_the_link(self):
        from django.core.management import call_command
        with mock.patch("core.services.storage.object_size", return_value=None), \
                mock.patch("core.services.storage.delete_object", return_value=False):
            out = io.StringIO()
            call_command("purge_videos", "--delete", stdout=out)
        self.assertIn("could not be removed from S3", out.getvalue())
        self.remote.refresh_from_db()
        self.assertIn("video.webm", json.loads(self.remote.artifacts_index_json))


class AnalyticsHeaderTests(TempDataMixin, TestCase):
    """2.17.1: the Analytics page is titled plainly -- no explanatory
    subtitle; the version table's "current" badge says which is live."""

    def test_no_subtitle_under_the_title(self):
        from django.urls import reverse
        page = self.client.get(reverse("analytics")).content.decode()
        self.assertIn("<h1>Analytics</h1>", page)
        self.assertNotIn("everything by WEBSITE version", page)
        self.assertNotIn("Currently testing", page)


class LivePageTests(TempDataMixin, TestCase):
    """2.18: pages refresh their run data in place (app.js hubLive) instead of
    reloading. What the server must provide: the data-live parts, in
    well-formed HTML, with the run/load pages frozen once finished."""

    def setUp(self):
        super().setUp()
        from core.models import Batch
        self.test = Test.objects.create(test_id="LV-1", file_path="lv.py")
        self.group = Group.objects.create(name="Live")
        self.group.tests.set([self.test])
        self.batch = Batch.objects.create(label="group: Live", trigger="group", group=self.group)
        self.done = Run.objects.create(test=self.test, batch=self.batch, status=Run.PASSED,
                                       duration_seconds=1.0, finished_at=djtimezone.now(),
                                       security_json=json.dumps({"findings": [{
                                           "id": "missing-csp", "severity": "medium",
                                           "title": "No Content-Security-Policy header",
                                           "detail": "d", "fix": "f"}]}))
        self.active = Run.objects.create(test=self.test, status=Run.RUNNING,
                                         started_at=djtimezone.now())

    def _html(self, name, *args):
        from django.urls import reverse
        resp = self.client.get(reverse(name, args=args))
        self.assertEqual(resp.status_code, 200, name)
        return resp.content.decode()

    def _balanced(self, html, where):
        """Every <div> inside <main> is closed: a stray </div> from a wrapper
        breaks the whole layout without any error."""
        main = html.split("<main>", 1)[1].split("</main>", 1)[0]
        import re
        opened = len(re.findall(r"<div[\s>]", main))
        closed = main.count("</div>")
        self.assertEqual(opened, closed, f"{where}: {opened} <div> vs {closed} </div>")

    def test_every_run_data_page_has_its_live_parts(self):
        from core.models import LoadRun
        load = LoadRun.objects.create(test=self.test, duration_seconds=60, status="finished")
        pages = {
            ("dashboard",): ["dash-tiles", "dash-new-failures", "dash-days", "dash-failing", "dash-next"],
            ("tests_list",): ["tests-table"],
            ("test_detail", "LV-1"): ["test"],
            ("runs_list",): ["runs-table"],
            ("batch_detail", self.batch.pk): ["batch"],
            ("groups_list",): ["groups-table", "group-history"],
            ("group_detail", self.group.pk): ["group-side"],
            ("schedules_list",): ["schedules-table", "schedule-history"],
            ("analytics",): ["analytics"],
            ("metrics",): ["metrics"],
            ("load_list",): ["load-runs"],
            ("load_detail", load.pk): ["load"],
            ("security_page",): ["security"],
            ("run_detail", self.done.pk): ["run"],
        }
        for (name, *args), keys in pages.items():
            html = self._html(name, *args)
            for key in keys:
                self.assertIn(f'data-live="{key}"', html, f"{name}: {key}")
            self._balanced(html, name)

    def test_the_batch_page_no_longer_reloads_itself(self):
        html = self._html("batch_detail", self.batch.pk)
        self.assertNotIn("location.reload", html)

    def test_a_finished_run_page_is_frozen_and_an_active_one_waits_for_its_live_view(self):
        import re
        done = re.search(r'<div data-live="run"([^>]*)>', self._html("run_detail", self.done.pk)).group(1)
        self.assertIn("data-live-manual", done)
        self.assertIn("data-live-frozen", done, "a replay being watched must not reset")
        active = re.search(r'<div data-live="run"([^>]*)>', self._html("run_detail", self.active.pk)).group(1)
        self.assertIn("data-live-manual", active)
        self.assertNotIn("data-live-frozen", active)

    def test_the_run_page_shows_its_security_observations(self):
        """The card sat after the content block's endblock, where Django never
        renders anything: the findings were computed and never shown."""
        html = self._html("run_detail", self.done.pk)
        self.assertIn("Security observations", html)
        self.assertIn("No Content-Security-Policy header", html)


class GroupForecastTests(TempDataMixin, TestCase):
    """2.19: the dashboard's Running now shows each group run as one block --
    done/left, and when its last test should finish, from the runner's own
    queue simulated over its worker slots."""

    @staticmethod
    def _run(pk, status, test_id, elapsed=None):
        from types import SimpleNamespace
        return SimpleNamespace(pk=pk, status=status, test_id=test_id, elapsed_seconds=elapsed)

    def test_the_queue_is_simulated_not_averaged(self):
        q = [self._run(1, Run.QUEUED, "a"), self._run(2, Run.QUEUED, "a"), self._run(3, Run.QUEUED, "a")]
        ends = stats.queue_forecast(q, 2, {"a": 10.0})
        self.assertEqual(ends, {1: 10.0, 2: 10.0, 3: 20.0}, "three 10 s runs on two slots end at 20 s")

    def test_queued_runs_wait_for_the_running_ones(self):
        q = [self._run(1, Run.RUNNING, "a", elapsed=4), self._run(2, Run.RUNNING, "b", elapsed=5),
             self._run(3, Run.QUEUED, "a"), self._run(4, Run.QUEUED, "a")]
        ends = stats.queue_forecast(q, 2, {"a": 10.0, "b": 20.0})
        self.assertEqual(ends, {1: 6.0, 2: 15.0, 3: 16.0, 4: 25.0})

    def test_no_history_means_no_guess(self):
        q = [self._run(1, Run.QUEUED, "new"), self._run(2, Run.QUEUED, "a")]
        ends = stats.queue_forecast(q, 1, {"new": None, "a": 10.0})
        self.assertEqual(ends, {1: None, 2: None}, "behind an unknown, the one slot's end is unknown")

    def _history(self, test, seconds, n=3):
        for _ in range(n):
            Run.objects.create(test=test, status=Run.PASSED, duration_seconds=seconds,
                               finished_at=djtimezone.now())

    def test_the_status_api_lists_group_runs_with_time_left(self):
        from core.models import Batch
        from django.urls import reverse
        self.cfg.raw["runner"]["max_parallel"] = 2
        a = Test.objects.create(test_id="G-A", file_path="a.py")
        self._history(a, 10.0)
        group = Group.objects.create(name="Nightly")
        batch = Batch.objects.create(label="group: Nightly", trigger="group", group=group)
        Run.objects.create(test=a, batch=batch, status=Run.PASSED, duration_seconds=10,
                           finished_at=djtimezone.now())
        Run.objects.create(test=a, batch=batch, status=Run.QUEUED)
        Run.objects.create(test=a, batch=batch, status=Run.QUEUED)
        Run.objects.create(test=a, batch=batch, status=Run.QUEUED)
        lone = Batch.objects.create(label="G-A", trigger="manual")
        Run.objects.create(test=a, batch=lone, status=Run.QUEUED)
        data = self.client.get(reverse("api_status")).json()
        self.assertEqual([b["id"] for b in data["active_batches"]], [batch.pk], "a lone run is not a group")
        g = data["active_batches"][0]
        self.assertEqual((g["group"], g["total"], g["done"], g["queued"], g["running"]),
                         ("Nightly", 4, 1, 3, 0))
        self.assertEqual(g["eta"], 20.0, "three 10 s tests on two slots")
        page = self.client.get(reverse("batch_detail", args=[batch.pk])).content.decode()
        self.assertIn("est. total remaining:\n    20s", page, "the batch page shows the same figure")

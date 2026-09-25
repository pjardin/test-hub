"""Unit tests for Acme Freight, the built-in demo site.

    python manage.py test freight         (./app/dev.sh test runs core + freight)

The demo is a TEST FIXTURE: its value is that each release behaves exactly as
its release notes say -- the right bug in the right version, the headers
present or missing on cue. These tests pin that behaviour, plus the hub-side
contract (runs stamped with the deployed version) and the upgrade path that
brings the new sample tests to an existing install.
"""
import ast
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core import appconfig
from core.models import Run, Test
from core.tests import TempDataMixin
from freight import catalog, geo, jobs, manifest, optimizer, pricing, releases, reports

ROOT = "/demo/freight/"
FREIGHT_DIR = Path(__file__).resolve().parent
SAMPLES = appconfig.APP_DIR / "sample_tests"


class FreightCase(TempDataMixin, TestCase):
    """Throwaway data dir (so the deployed release is private to the test)
    and jobs/queries that do not actually sleep."""

    def setUp(self):
        super().setUp()
        self._scale = jobs.TIME_SCALE
        jobs.TIME_SCALE = 0
        # in-memory demo state (what a restart clears) must not leak between tests
        from freight import accounts, mail
        accounts.reset_all()
        mail.clear()

    def tearDown(self):
        jobs.TIME_SCALE = self._scale
        super().tearDown()

    def deploy(self, version):
        releases.deploy(version, by="unit test")

    def sign_in(self, user="tester"):
        self.client.get(ROOT + "login/")
        resp = self.client.post(ROOT + "login/", {"username": user,
                                                  "password": "demo-password"})
        self.assertEqual(resp.status_code, 302, "sign-in failed")

    def quote_domestic(self, promo=""):
        c = self.client
        c.get(ROOT + "quote/?restart=1")
        self.assertEqual(c.post(ROOT + "quote/route/", {
            "origin": "San Francisco", "destination": "San Diego"}).status_code, 302)
        self.assertEqual(c.post(ROOT + "quote/cargo/", {
            "weight_kg": "120", "pieces": "3"}).status_code, 302)
        self.assertEqual(c.post(ROOT + "quote/service/", {"service": "standard"}).status_code, 302)
        if promo:
            c.post(ROOT + "quote/review/", {"action": "promo", "promo": promo})
        return c.get(ROOT + "quote/review/")

    @staticmethod
    def amount(resp, element_id):
        found = re.search(rf'id="{element_id}" data-amount="([\d.]+)"', resp.content.decode())
        return float(found.group(1)) if found else None


# --------------------------------------------------------------------------
# releases, and the hub stamping runs with the deployed one
# --------------------------------------------------------------------------

class ReleaseTests(FreightCase):

    def test_the_first_release_is_what_appconfig_assumes(self):
        self.assertEqual(releases.FIRST.version, appconfig.DEMO_FIRST_VERSION)

    def test_nothing_deployed_means_the_first_release(self):
        self.assertEqual(releases.deployed().version, "1.0.0")
        self.assertEqual(self.cfg.target_version, "1.0.0")

    def test_runs_are_stamped_with_the_deployed_release(self):
        """The whole analytics story: a run records the release it tested --
        also when queued from the terminal (another process, another
        container in the docker image), hence the state FILE."""
        from core.services import terminal
        test = Test.objects.create(test_id="FR-1", name="x", file_path="FR-1__x.py")
        self.deploy("2.1.0")
        run = terminal.enqueue_cli([test])[0]
        self.assertEqual(run.target_version, "2.1.0")
        self.deploy("3.0.0")
        self.assertEqual(terminal.enqueue_cli([test])[0].target_version, "3.0.0")

    def test_a_real_target_keeps_its_configured_version(self):
        self.cfg.raw["target"]["url"] = "https://app.example.internal/"
        self.cfg.raw["target"]["version"] = "7.4"
        self.deploy("2.0.0")
        self.assertEqual(self.cfg.target_version, "7.4")

    def test_config_version_is_ignored_for_the_demo(self):
        """config.example.json ships target.version "1.0.0"; for the demo that
        would label every run 1.0.0 forever, whatever is deployed."""
        self.cfg.raw["target"]["version"] = "1.0.0"
        self.deploy("2.0.0")
        self.assertEqual(self.cfg.target_version, "2.0.0")
        self.assertEqual(self.cfg.configured_target_version, "1.0.0")

    def test_a_garbled_state_file_falls_back_to_the_first_release(self):
        path = self.cfg.demo_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")
        self.assertEqual(releases.deployed().version, "1.0.0")
        path.write_text(json.dumps({"version": "9.9.9"}))
        self.assertEqual(releases.deployed().version, "1.0.0")

    def test_unknown_release_is_refused_and_history_is_kept(self):
        with self.assertRaises(ValueError):
            releases.deploy("4.0.0")
        self.deploy("1.1.0")
        self.deploy("2.0.0")
        self.assertEqual([h["version"] for h in releases.deploy_history()], ["2.0.0", "1.1.0"])
        self.assertEqual(list(self.cfg.demo_state_path.parent.glob(".release-*")), [],
                         "a temp file from the atomic write was left behind")

    def test_every_release_sets_every_flag(self):
        for rel in releases.RELEASES:
            self.assertEqual(set(rel.flags), set(releases.BASE_FLAGS), rel.version)
            self.assertTrue(rel.spoilers, f"{rel.version} must say what the hub should notice")

    def test_the_console_deploys_and_the_terminal_command_too(self):
        resp = self.client.post(ROOT + "releases/", {"version": "2.0.0"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["X-Demo-Version"], "2.0.0", "the redirect should already speak 2.0.0")
        out = io.StringIO()
        call_command("demo_release", "3.0.0", stdout=out)
        self.assertIn("deployed Acme Freight 3.0.0", out.getvalue())
        self.assertEqual(releases.deployed().version, "3.0.0")
        with self.assertRaises(CommandError):
            call_command("demo_release", "0.1", stdout=io.StringIO())


# --------------------------------------------------------------------------
# each release plants exactly its own problems
# --------------------------------------------------------------------------

class PlantedBehaviourTests(FreightCase):

    def test_security_headers_by_release(self):
        for rel in releases.RELEASES:
            self.deploy(rel.version)
            resp = self.client.get(ROOT)
            broken = rel.version == "2.1.0"
            self.assertEqual(resp["X-Demo-Version"], rel.version)
            self.assertEqual("Content-Security-Policy" in resp, not broken, rel.version)
            self.assertEqual("X-Frame-Options" in resp, not broken, rel.version)
            self.assertEqual(b"acme-metrics.invalid" in resp.content, broken, rel.version)
            self.assertIn(f'<meta name="app-version" content="{rel.version}">', resp.content.decode())

    def test_theme_changes_with_the_redesign(self):
        for version, theme in (("1.1.0", "classic"), ("2.0.0", "modern"), ("3.0.0", "modern")):
            self.deploy(version)
            self.assertRegex(self.client.get(ROOT).content.decode(),
                             rf'<body class="theme-{theme}[ "]')

    def test_session_rotation_is_lost_only_in_2_1(self):
        for rel in releases.RELEASES:
            self.deploy(rel.version)
            self.client.cookies.clear()
            self.client.get(ROOT + "login/")
            before = self.client.cookies["sessionid"].value
            self.sign_in()
            rotated = self.client.cookies["sessionid"].value != before
            self.assertEqual(rotated, rel.version != "2.1.0", rel.version)

    def test_sign_out_kills_the_old_session(self):
        self.sign_in()
        old = self.client.cookies["sessionid"].value
        self.client.get(ROOT + "logout/")
        self.client.cookies["sessionid"] = old            # replay the old cookie
        self.assertEqual(self.client.get(ROOT + "ops/").status_code, 302)

    def test_promo_is_applied_twice_only_in_2_1(self):
        for rel in releases.RELEASES:
            self.deploy(rel.version)
            resp = self.quote_domestic(promo="SPRING10")
            subtotal, discount = self.amount(resp, "price-subtotal"), self.amount(resp, "price-discount")
            factor = 0.20 if rel.version == "2.1.0" else 0.10
            self.assertAlmostEqual(discount, round(subtotal * factor, 2), places=2, msg=rel.version)

    def test_notes_fail_every_third_save_only_in_2_0(self):
        for version, want in (("1.0.0", 0), ("2.0.0", 2), ("3.0.0", 0)):
            self.deploy(version)
            self.sign_in()
            codes = [self.client.post(ROOT + "api/shipments/AF-100001/notes/",
                                      data=json.dumps({"text": f"n{i}"}),
                                      content_type="application/json").status_code
                     for i in range(6)]
            self.assertEqual(codes.count(503), want, f"{version}: {codes}")
            self.assertEqual(codes.count(201), 6 - want, f"{version}: {codes}")

    def test_customs_step_only_for_international_from_2_0(self):
        for version, want in (("1.1.0", False), ("2.0.0", True)):
            self.deploy(version)
            c = self.client
            c.post(ROOT + "quote/route/", {"origin": "Chicago", "destination": "Frankfurt"})
            nxt = c.post(ROOT + "quote/cargo/", {"weight_kg": "50", "pieces": "1"})["Location"]
            self.assertEqual("customs" in nxt, want, version)
        # domestic never asks
        c.post(ROOT + "quote/route/", {"origin": "Dallas", "destination": "Houston"})
        nxt = c.post(ROOT + "quote/cargo/", {"weight_kg": "50", "pieces": "1"})["Location"]
        self.assertNotIn("customs", nxt)

    def test_the_optimizer_step_is_twice_as_slow_in_2_0(self):
        speeds = {r.version: r.flags["optimizer_step_s"] for r in releases.RELEASES}
        self.assertAlmostEqual(speeds["2.0.0"] / speeds["1.0.0"], 2.1, places=1)
        self.assertLess(speeds["3.0.0"], speeds["1.0.0"])


# --------------------------------------------------------------------------
# pages, forms and the staff portal
# --------------------------------------------------------------------------

class PageTests(FreightCase):
    PUBLIC = ["", "track/", "track/?id=AF-100001", "track/?id=AF-000000", "tour/",
              "releases/", "login/", "quote/route/", "map/world.svg",
              "map/central_europe.svg?theme=modern", "api/version/", "api/cities/?q=port",
              "sample-manifest.csv"]
    STAFF = ["ops/", "ops/shipments/", "ops/shipments/?q=lumen&status=in_transit&sort=-eta",
             "ops/shipments/?sort=status&page=9", "ops/shipments/AF-100003/",
             "ops/optimizer/", "ops/import/"]

    def test_every_page_renders_in_every_release(self):
        for rel in releases.RELEASES:
            self.deploy(rel.version)
            self.client.cookies.clear()
            for path in self.PUBLIC:
                self.assertEqual(self.client.get(ROOT + path).status_code, 200, f"{rel.version} {path}")
            self.sign_in("manager")
            for path in self.STAFF + ["ops/reports/"]:
                self.assertEqual(self.client.get(ROOT + path).status_code, 200, f"{rel.version} {path}")

    def test_staff_pages_need_a_sign_in(self):
        for path in self.STAFF:
            resp = self.client.get(ROOT + path)
            self.assertEqual(resp.status_code, 302, path)
            self.assertIn("/demo/freight/login/?next=", resp["Location"])
        self.assertEqual(self.client.get(ROOT + "api/jobs/nope/").status_code, 401)

    def test_signed_in_responses_are_never_cached(self):
        self.sign_in("manager")
        for path in self.STAFF + ["ops/reports/"]:
            self.assertEqual(self.client.get(ROOT + path).get("Cache-Control"), "no-store", path)
        self.assertNotEqual(self.client.get(ROOT).get("Cache-Control"), "no-store",
                            "public pages may be cached")

    def test_reports_are_for_managers(self):
        self.sign_in("tester")
        resp = self.client.get(ROOT + "ops/reports/")
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b'id="forbidden"', resp.content)

    def test_sign_in_never_redirects_off_site(self):
        self.client.get(ROOT + "login/")
        resp = self.client.post(ROOT + "login/", {"username": "tester", "password": "demo-password",
                                                 "next": "https://evil.example/"})
        self.assertEqual(resp["Location"], ROOT + "ops/")
        self.client.cookies.clear()
        resp = self.client.post(ROOT + "login/", {"username": "tester", "password": "nope"})
        self.assertEqual(resp.status_code, 401)

    def test_tracking(self):
        resp = self.client.get(ROOT + "track/?id=af-100001")        # case-insensitive
        self.assertIn(b'id="track-status"', resp.content)
        self.assertIn(b"In transit", resp.content)
        self.assertIn(b'id="track-not-found"', self.client.get(ROOT + "track/?id=AF-1").content)

    def test_quote_wizard_books_a_trackable_shipment(self):
        self.quote_domestic(promo="SPRING10")
        resp = self.client.post(ROOT + "quote/review/", {
            "action": "book", "contact_name": "Ada", "contact_email": "ada@example.gov"})
        self.assertEqual(resp.status_code, 302)
        booking_id = resp["Location"].rstrip("/").rsplit("/", 1)[-1]
        self.assertRegex(booking_id, r"^AF-9\d{5}$")
        track = self.client.get(ROOT + f"track/?id={booking_id}")
        self.assertIn(b"Booked", track.content)

    def test_wizard_steps_cannot_be_skipped(self):
        self.client.get(ROOT + "quote/?restart=1")
        for step in ("cargo", "service", "review"):
            resp = self.client.get(ROOT + f"quote/{step}/")
            self.assertEqual(resp["Location"], ROOT + "quote/route/", step)

    def test_wizard_validation_messages(self):
        resp = self.client.post(ROOT + "quote/route/", {"origin": "Sacremento", "destination": ""})
        self.assertEqual(resp.status_code, 400)
        body = resp.content.decode()
        self.assertIn("did you mean Sacramento", body)
        self.assertIn("Required", body)
        self.client.post(ROOT + "quote/route/", {"origin": "Dallas", "destination": "Houston"})
        resp = self.client.post(ROOT + "quote/cargo/", {"weight_kg": "-3", "pieces": "0"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b'id="weight-error"', resp.content)

    def test_hazardous_goods_cannot_fly_express(self):
        self.client.post(ROOT + "quote/route/", {"origin": "Dallas", "destination": "Houston"})
        self.client.post(ROOT + "quote/cargo/", {"weight_kg": "10", "pieces": "1", "hazardous": "1"})
        resp = self.client.post(ROOT + "quote/service/", {"service": "express"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"cannot fly Express", resp.content)

    def test_shipments_table_filters_sorts_and_pages(self):
        self.sign_in()
        resp = self.client.get(ROOT + "ops/shipments/?status=exception")
        body = resp.content.decode()
        self.assertNotIn("status-in_transit", body.split('id="shipments-table"')[1])
        resp = self.client.get(ROOT + "ops/shipments/?q=AF-100001")
        self.assertIn("Showing 1&ndash;1 of 1 shipment", resp.content.decode())
        resp = self.client.get(ROOT + "ops/shipments/?sort=-id&page=999")      # clamps
        self.assertEqual(resp.status_code, 200)

    def test_notes_api_validates(self):
        url = ROOT + "api/shipments/AF-100001/notes/"
        self.assertEqual(self.client.post(url, data="{}", content_type="application/json").status_code, 401)
        self.sign_in()
        self.assertEqual(self.client.post(url, data=json.dumps({"text": " "}),
                                          content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(url, data=json.dumps({"text": "x" * 501}),
                                          content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(ROOT + "api/shipments/AF-1/notes/",
                                          data=json.dumps({"text": "hi"}),
                                          content_type="application/json").status_code, 404)

    def test_deliver_and_flag_actions(self):
        self.sign_in()
        self.client.post(ROOT + "ops/shipments/AF-100001/", {"action": "deliver"})
        self.assertIn(b">Delivered<", self.client.get(ROOT + "ops/shipments/AF-100001/").content)
        self.client.post(ROOT + "ops/shipments/AF-100004/", {"action": "flag", "reason": "weather"})
        page = self.client.get(ROOT + "ops/shipments/AF-100004/").content
        self.assertIn(b">Exception<", page)
        self.assertIn(b"Weather delay", page)


# --------------------------------------------------------------------------
# the slow jobs, run instantly
# --------------------------------------------------------------------------

class JobTests(FreightCase):

    def run_job(self, fn, params, kind="t"):
        job = jobs.submit(kind, "test job", fn, params, "1.0.0")
        self.assertTrue(jobs.wait(job, 60), "job did not finish")
        self.assertEqual(job.state, jobs.DONE, job.error)
        return job

    def test_optimizer_plans_are_valid_and_deterministic(self):
        params = {"region": "texas", "stops": 20, "trucks": 3, "seed": 7, "step_s": 0.5}
        a = self.run_job(optimizer.run, params).result
        b = self.run_job(optimizer.run, params).result
        self.assertEqual(a["best_km"], b["best_km"], "same seed, same plan")
        stops = sorted(s for route in a["routes"] for s in route["stops"])
        self.assertEqual(stops, sorted(p[0] for p in a["stop_points"]), "every stop exactly once")
        self.assertTrue(all(r["load"] <= a["capacity"] for r in a["routes"]))
        self.assertGreater(a["saving_pct"], 25)
        self.assertEqual(len(optimizer.plan_csv(a).splitlines()), 21)

    def test_optimizer_best_line_never_goes_up(self):
        job = self.run_job(optimizer.run, {"region": "california", "stops": 15, "trucks": 3,
                                           "seed": 1, "step_s": 0.5})
        best = [p[2] for p in job.snapshot()["series"]]
        self.assertTrue(all(b <= a + 1e-6 for a, b in zip(best, best[1:])), best)

    def test_every_region_has_a_depot_and_enough_stops(self):
        for key in catalog.REGIONS:
            self.assertIsNotNone(catalog.depot(key), key)
            self.assertGreaterEqual(len(catalog.region_stops(key)), optimizer.STOPS_RANGE[1], key)

    def test_sample_manifest_has_exactly_its_five_mistakes(self):
        rows = manifest.parse((catalog.DATA_DIR / "sample_manifest.csv").read_bytes())
        result = self.run_job(manifest.run, {"rows": rows}).result
        self.assertEqual((result["ok"], result["errors"]), (35, 5))
        problems = " | ".join(m for r in result["rows"] for m in r["messages"])
        for expected in ("did you mean Springfield", "is not between 0 and",
                         "appears twice", "destination: missing", "cannot fly Express"):
            self.assertIn(expected, problems)

    def test_manifest_file_problems_are_explained(self):
        with self.assertRaisesRegex(manifest.ManifestError, "Missing column"):
            manifest.parse(b"ref,from\nA,B\n")
        with self.assertRaisesRegex(manifest.ManifestError, "not UTF-8"):
            manifest.parse("reference\n\xff".encode("latin-1"))
        with self.assertRaisesRegex(manifest.ManifestError, "limit"):
            manifest.parse(b"x" * (manifest.MAX_BYTES + 1))

    def test_report_matches_the_shipments(self):
        month = reports.available_months()[0][0]
        result = self.run_job(reports.run, {"month": month, "detail": "summary"}).result
        expected = [s for s in catalog.shipments() if s.created.strftime("%Y-%m") == month]
        self.assertEqual(result["kpis"]["shipments"], len(expected))
        self.assertEqual(sum(r["shipments"] for r in result["by_service"]), len(expected))

    def test_cancel_stops_a_job(self):
        jobs.TIME_SCALE = 1.0
        job = jobs.submit("t", "slow", reports.run,
                          {"month": reports.available_months()[0][0], "detail": "full",
                           "unit_s": 5}, "1.0.0")
        job.cancel()
        self.assertTrue(jobs.wait(job, 10))
        self.assertEqual(job.state, jobs.CANCELLED)

    def test_job_pages_end_to_end(self):
        self.sign_in("manager")
        cases = [("ops/optimizer/", {"region": "central_europe", "stops": "8", "trucks": "2"}, b'id="routes-table"'),
                 ("ops/reports/", {"month": reports.available_months()[0][0], "detail": "summary"}, b'id="report-title"')]
        for path, form, marker in cases:
            resp = self.client.post(ROOT + path, form)
            self.assertEqual(resp.status_code, 302, path)
            job = jobs.get(resp["Location"].rstrip("/").rsplit("/", 1)[-1])
            jobs.wait(job, 60)
            page = self.client.get(resp["Location"])
            self.assertIn(marker, page.content)
            download = self.client.get(resp["Location"] + "download/")
            self.assertEqual(download["Content-Type"], "text/csv; charset=utf-8")
            status = self.client.get(ROOT + f"api/jobs/{job.id}/?log=0&series=0").json()
            self.assertEqual(status["state"], "done")
        with open(catalog.DATA_DIR / "sample_manifest.csv", "rb") as fh:
            resp = self.client.post(ROOT + "ops/import/", {"manifest": fh})
        jobs.wait(jobs.get(resp["Location"].rstrip("/").rsplit("/", 1)[-1]), 60)
        self.assertIn(b'id="import-errors">5<', self.client.get(resp["Location"]).content)

    def test_bad_optimizer_input_is_refused(self):
        self.sign_in()
        resp = self.client.post(ROOT + "ops/optimizer/", {"region": "mars", "stops": "500", "trucks": "0"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.get(ROOT + "ops/jobs/doesnotexist/").status_code, 404)


# --------------------------------------------------------------------------
# data, maps, money
# --------------------------------------------------------------------------

class CatalogTests(TestCase):

    def test_city_lookup_the_way_people_type_it(self):
        cases = {"Fort Worth": "Ft. Worth", "Saint Louis": "St. Louis", "sao paulo": "São Paulo",
                 "Portland": "Portland", "Portland, Maine": "Portland"}
        for text, name in cases.items():
            city = catalog.find_city(text)
            self.assertIsNotNone(city, text)
            self.assertEqual(city.name, name, text)
        self.assertEqual(catalog.find_city("Portland").admin1, "Oregon",
                         "same name, same country: the bigger city wins (and both exist)")
        self.assertEqual(catalog.find_city("Portland, Maine").admin1, "Maine")
        self.assertIsNone(catalog.find_city("Atlantis"))
        self.assertIn("Sacramento", catalog.did_you_mean("Sacremento"))

    def test_shipments_are_deterministic_and_never_in_the_future(self):
        import datetime as dt
        first = [(s.id, str(s.price), s.status) for s in catalog._generate(catalog.today())]
        again = [(s.id, str(s.price), s.status) for s in catalog._generate(catalog.today())]
        self.assertEqual(first, again)
        now = dt.datetime.now()
        self.assertFalse([s.id for s in catalog.shipments() if s.events[-1][1] > now])
        for sid in catalog.FIXTURES:
            self.assertIsNotNone(catalog.shipment(sid), sid)

    def test_no_scan_is_in_the_future_at_any_hour(self):
        # the snapshot is taken at 09:00; before 9 a.m. the machine shows
        # YESTERDAY's -- a calendar-day snapshot put scans later that same
        # morning in front of anyone looking between midnight and 8:15
        import datetime as dt
        day = dt.date(2026, 9, 25)
        for hh, mm in ((0, 5), (8, 14), (9, 0), (15, 30), (23, 59)):
            at = dt.datetime.combine(day, dt.time(hh, mm))
            snap = catalog.snapshot_day(at)
            self.assertEqual(snap, day if hh >= 9 else day - dt.timedelta(days=1), at)
            late = [s.id for s in catalog._generate(snap)
                    if max(when for _, when, _, _ in s.events) > at or s.created > at]
            self.assertFalse(late, f"at {at:%H:%M} these shipments show scans from later on: {late[:5]}")

    def test_history_only_uses_the_original_hub_network(self):
        new_hubs = {h.code for h in catalog.hubs() if h.since != "1.0.0"}
        used = {s.origin_hub.code for s in catalog.shipments()} | {s.dest_hub.code for s in catalog.shipments()}
        self.assertFalse(used & new_hubs, "shipments that 'happened' before 2.0 use 2.0's hubs")

    def test_money_is_exact(self):
        q = pricing.quote(catalog.find_city("San Francisco"), catalog.find_city("San Diego"),
                          120, "standard", promo="SPRING10")
        self.assertEqual(q.subtotal, sum(line.amount for line in q.lines))
        self.assertEqual(q.total, q.subtotal - q.discount + q.fuel)
        self.assertEqual(str(q.discount), "11.76")
        bulky = pricing.chargeable_kg(10, dims_cm=[100, 100, 100])
        self.assertEqual(str(bulky), "200.0", "a light but bulky box pays for its volume")

    def test_basemaps_are_well_formed_svg(self):
        for key in ["world"] + list(catalog.REGIONS):
            for theme in geo.THEMES:
                root = ET.fromstring(geo.basemap_svg(key, theme))
                self.assertTrue(root.tag.endswith("svg"), key)
            proj = geo.projection(key)
            if key != "world":
                depot = catalog.depot(key)
                x, y = proj.xy(depot.lat, depot.lon)
                self.assertTrue(0 <= x <= proj.width and 0 <= y <= proj.height, key)


# --------------------------------------------------------------------------
# air gap, password gate, sample tests
# --------------------------------------------------------------------------

class AirGapTests(FreightCase):

    def test_no_external_urls_except_the_planted_tracker(self):
        """The hub's rule: nothing in the UI reaches the network. The demo has
        ONE deliberate exception -- release 2.1.0's tracker, on a .invalid
        host that can never resolve -- so the Security page has a real
        external request to catch."""
        allowed = {"https://analytics.acme-metrics.invalid/collect.js",
                   "http://www.w3.org/2000/svg"}
        found = set()
        for path in FREIGHT_DIR.rglob("*"):
            if path.suffix in (".html", ".js", ".css", ".py") and path.name != "tests.py":
                found |= set(re.findall(r"https?://[^\s\"'<>)]+", path.read_text(encoding="utf-8")))
        self.assertEqual(found - allowed, set(), "an external URL crept into the demo site")

    def test_demo_is_reachable_behind_the_password_gate(self):
        self.cfg.raw["site"]["password"] = "secret"
        for path in ("", "track/?id=AF-100001", "login/", "releases/", "api/version/"):
            self.assertEqual(self.client.get(ROOT + path).status_code, 200, path)
        self.assertEqual(self.client.get("/tests/").status_code, 302, "the gate must stay shut")

    def test_the_prefix_exemption_cannot_be_walked_out_of(self):
        self.cfg.raw["site"]["password"] = "secret"
        for path in ("/demo/freight/../../tests/", "/demo/freight/%2e%2e/%2e%2e/settings/",
                     "/demo/freight/../../artifacts/x/run.log"):
            resp = self.client.get(path)
            self.assertNotEqual(resp.status_code, 200, path)
            self.assertNotIn(b"Settings", resp.content[:4000], path)


class SampleTestFileTests(TestCase):

    def test_freight_samples_parse_and_their_sidecars_match(self):
        for py in sorted(list(SAMPLES.glob("DEMO-01*.py")) + list(SAMPLES.glob("DEMO-02*.py"))):
            ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            sidecar = json.loads(py.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["id"], py.name.split("__")[0])

    def test_groups_only_name_tests_that_ship(self):
        ids = {p.name.split("__")[0] for p in SAMPLES.iterdir() if "__" in p.name}
        groups = json.loads((SAMPLES / "_groups.json").read_text())["groups"]
        for group in groups:
            self.assertFalse(set(group["tests"]) - ids, group["name"])


class SamplesCommandTests(TempDataMixin, TestCase):
    """An upgrade never touches the tests folder, so new samples need a way
    in that can never overwrite anything."""

    def test_adds_only_what_is_missing(self):
        tests = self.tests_dir
        (tests / "DEMO-001__homepage_loads.py").write_text("# my own edits\n")
        (tests / "_archive").mkdir()
        (tests / "_archive" / "DEMO-013__old.py").write_text("# archived on purpose\n")
        (tests / "_groups.json").write_text(json.dumps({"groups": [
            {"name": "Mine", "tests": ["DEMO-001"]},
            {"name": "Freight smoke", "tests": ["DEMO-001"]}]}))
        out = io.StringIO()
        call_command("samples", stdout=out)
        self.assertIn("--add-new", out.getvalue())
        self.assertFalse((tests / "DEMO-010__freight_home_and_tracking.py").exists(),
                         "listing must not copy anything")

        call_command("samples", "--add-new", stdout=io.StringIO())
        self.assertEqual((tests / "DEMO-001__homepage_loads.py").read_text(), "# my own edits\n")
        self.assertTrue((tests / "DEMO-010__freight_home_and_tracking.json").exists())
        self.assertTrue((tests / "_lib" / "freight.py").exists())
        self.assertFalse((tests / "DEMO-013__freight_shipments_and_notes.py").exists(),
                         "an archived test must not be resurrected")
        names = [g["name"] for g in json.loads((tests / "_groups.json").read_text())["groups"]]
        self.assertIn("Mine", names)
        self.assertIn("Freight regression", names)
        self.assertEqual(names.count("Freight smoke"), 1, "an existing group was duplicated")
        self.assertTrue(Test.objects.filter(test_id="DEMO-014").exists(), "not rescanned")

        again = io.StringIO()
        call_command("samples", stdout=again)
        self.assertIn("nothing to add", again.getvalue())

    def test_changed_sample_code_is_replaced_only_on_request_and_kept(self):
        """A 2.13 hub has 2.13's _lib/freight.py and DEMO-011 -- possibly with
        the lab's own edits. --add-new must leave them; --update swaps them
        and keeps theirs; sidecars are never replaced."""
        tests, lib = self.tests_dir, self.tests_dir / "_lib"
        lib.mkdir(parents=True)
        mine = "# an older copy, with my own helper\n"
        (lib / "freight.py").write_text(mine)
        old_test = "def run(page, ctx):\n    page.goto(ctx.base_url)\n"
        (tests / "DEMO-011__freight_quote_with_promo.py").write_text(old_test)
        my_sidecar = json.dumps({"id": "DEMO-011", "name": "my name", "timeout_seconds": 999})
        (tests / "DEMO-011__freight_quote_with_promo.json").write_text(my_sidecar)
        out = io.StringIO()
        call_command("samples", "--add-new", stdout=out)
        self.assertIn("lib*   _lib/freight.py", out.getvalue())
        self.assertIn("test*  DEMO-011__freight_quote_with_promo.py", out.getvalue())
        self.assertEqual((lib / "freight.py").read_text(), mine, "--add-new overwrote a file")
        self.assertEqual((tests / "DEMO-011__freight_quote_with_promo.py").read_text(), old_test)
        self.assertTrue((lib / "pages.py").exists(), "a MISSING shared file is still copied")

        out = io.StringIO()
        call_command("samples", "--update", stdout=out)
        for rel, before in (("_lib/freight.py", mine),
                            ("DEMO-011__freight_quote_with_promo.py", old_test)):
            here = tests / rel
            self.assertEqual(here.read_bytes(), (SAMPLES / rel).read_bytes(), rel)
            kept = sorted(here.parent.glob(here.name + ".before-*"))
            self.assertEqual([k.read_text() for k in kept], [before], out.getvalue())
            self.assertIn(kept[0].name, out.getvalue(), "the output must say where yours went")
        self.assertFalse(list(tests.rglob(".*.new")), "a temp file was left behind")
        self.assertEqual((tests / "DEMO-011__freight_quote_with_promo.json").read_text(),
                         my_sidecar, "a sidecar (the lab's settings) was replaced")
        self.assertEqual(Test.objects.get(test_id="DEMO-011").timeout_seconds, 999)
        self.assertEqual(Test.objects.filter(test_id__startswith="DEMO-011").count(), 1,
                         "the kept copy was synced as a test")

        again = io.StringIO()
        call_command("samples", stdout=again)
        self.assertIn("nothing to add", again.getvalue())


# --------------------------------------------------------------------------
# 2.14: incidents, the public API, dock scheduling, the live fleet
# --------------------------------------------------------------------------

class IncidentTests(FreightCase):

    def test_an_outage_takes_the_site_down_but_not_the_console(self):
        releases.set_incident("outage")
        home = self.client.get(ROOT)
        self.assertEqual(home.status_code, 503)
        self.assertEqual(home["Retry-After"], "300")
        self.assertIn(b'id="maintenance"', home.content)
        self.assertEqual(self.client.get(ROOT + "releases/").status_code, 200,
                         "the console must stay up, or nobody can end the outage")
        api = self.client.get(ROOT + "api/v1/status")
        self.assertEqual(api.status_code, 503)
        self.assertIn("maintenance", api.json()["error"])
        self.assertEqual(self.client.get("/demo/").status_code, 200, "the classic demo is not Acme")

    def test_slow_still_serves_and_clear_ends_it(self):
        releases.set_incident("slow")
        self.assertEqual(self.client.get(ROOT).status_code, 200)
        self.client.post(ROOT + "releases/", {"action": "incident", "incident": "clear"})
        self.assertIsNone(releases.incident())
        self.assertEqual([h["what"] for h in releases.deploy_history()[:2]], ["clear", "slow"])

    def test_a_deploy_keeps_the_incident_and_vice_versa(self):
        releases.set_incident("outage")
        self.deploy("2.0.0")
        self.assertEqual(releases.incident(), "outage")
        releases.set_incident(None)
        self.assertEqual(releases.deployed().version, "2.0.0")
        with self.assertRaises(ValueError):
            releases.set_incident("meteor")

    def test_the_terminal_switch(self):
        out = io.StringIO()
        call_command("demo_release", "--incident", "outage", stdout=out)
        self.assertIn("INCIDENT IN PROGRESS", out.getvalue())
        call_command("demo_release", "--incident", "clear", stdout=io.StringIO())
        self.assertIsNone(releases.incident())


class PublicApiTests(FreightCase):
    DEMO, TRIAL = "acme-demo-7f3a91c2", "acme-trial-2b91e4d0"

    def setUp(self):
        super().setUp()
        from freight import public_api
        public_api.limiter.reset()

    def get(self, path, key=DEMO):
        extra = {"HTTP_X_API_KEY": key} if key else {}
        return self.client.get(ROOT + "api/v1/" + path, **extra)

    def quote(self, body, key=DEMO):
        return self.client.post(ROOT + "api/v1/quotes", data=json.dumps(body),
                                content_type="application/json", HTTP_X_API_KEY=key)

    def test_keys(self):
        self.assertEqual(self.get("shipments/AF-100001", key="").status_code, 401)
        self.assertEqual(self.get("shipments/AF-100001", key="nope").status_code, 401)
        self.assertEqual(self.get("shipments/AF-100001").status_code, 200)
        self.assertEqual(self.get("status", key="").status_code, 200, "status is public")

    def test_the_contract_changes_by_release(self):
        expected = {"1.0.0": {"eta"}, "1.1.0": {"eta"}, "2.0.0": {"estimated_delivery"},
                    "2.1.0": {"estimated_delivery"},
                    "3.0.0": {"eta", "estimated_delivery", "deprecated"}}
        for version, fields in expected.items():
            self.deploy(version)
            body = self.get("shipments/AF-100001").json()
            present = {f for f in ("eta", "estimated_delivery", "deprecated") if f in body}
            self.assertEqual(present, fields, version)

    def test_money_is_exact_strings_and_the_promo_bug_reaches_the_api(self):
        for version, factor in (("1.0.0", "0.10"), ("2.1.0", "0.20")):
            self.deploy(version)
            q = self.quote({"origin": "San Francisco", "destination": "San Diego",
                            "weight_kg": 120, "promo": "SPRING10"}).json()
            self.assertIsInstance(q["total"], str)
            from decimal import Decimal
            self.assertEqual(Decimal(q["discount"]),
                             (Decimal(q["subtotal"]) * Decimal(factor)).quantize(Decimal("0.01")),
                             version)

    def test_the_trial_key_is_limited_except_on_2_1(self):
        for version, limited in (("1.0.0", True), ("2.1.0", False), ("3.0.0", True)):
            self.deploy(version)
            from freight import public_api
            public_api.limiter.reset()
            answers = [self.get("shipments/AF-100002", key=self.TRIAL) for _ in range(6)]
            self.assertEqual(answers[-1].status_code == 429, limited, version)
            if limited:
                self.assertTrue(answers[-1]["Retry-After"].isdigit())
                self.assertEqual([a.status_code for a in answers[:5]], [200] * 5)

    def test_validation_names_the_field(self):
        self.assertEqual(self.get("shipments?status=lost").status_code, 400)
        self.assertEqual(self.get("shipments?limit=500").status_code, 400)
        page = self.get("shipments?status=exception&limit=3").json()
        self.assertEqual(len(page["results"]), 3)
        self.assertTrue(all(r["status"] == "exception" for r in page["results"]))
        self.assertEqual(self.get("shipments/AF-1").status_code, 404)
        bad_city = self.quote({"origin": "Sacremento", "destination": "Reno", "weight_kg": 5}).json()
        self.assertEqual(bad_city["field"], "origin")
        self.assertIn("Sacramento", bad_city["did_you_mean"])
        hazmat = self.quote({"origin": "Reno", "destination": "Fresno", "weight_kg": 5,
                             "service": "express", "hazardous": True})
        self.assertEqual(hazmat.status_code, 400)
        garbage = self.client.post(ROOT + "api/v1/quotes", data="[1, 2]",
                                   content_type="application/json", HTTP_X_API_KEY=self.DEMO)
        self.assertEqual(garbage.status_code, 400)

    def test_api_clients_need_no_csrf_token(self):
        """A partner's server has no browser cookie to echo -- the key is the auth."""
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        r = strict.post(ROOT + "api/v1/quotes", data=json.dumps(
            {"origin": "Reno", "destination": "Fresno", "weight_kg": 5}),
            content_type="application/json", HTTP_X_API_KEY=self.DEMO)
        self.assertEqual(r.status_code, 200)

    def test_developers_page_shows_only_the_changelog_so_far(self):
        self.deploy("2.1.0")
        page = self.client.get(ROOT + "developers/").content.decode()
        self.assertIn("API v1 launched", page)
        self.assertNotIn("Restored", page, "2.0.0's rename was never announced")
        self.deploy("3.0.0")
        self.assertIn("Restored", self.client.get(ROOT + "developers/").content.decode())


class DockTests(FreightCase):

    def board(self):
        return self.client.get(ROOT + "ops/docks/").content.decode()

    def post(self, body):
        return self.client.post(ROOT + "api/docks/", data=json.dumps(body),
                                content_type="application/json")

    def test_the_yards_rules(self):
        from freight import docks
        self.sign_in()
        arrivals = docks.arrivals()
        hazmat = next(a for a in arrivals if a.hazardous)
        heavy = next(a for a in arrivals if a.heavy and not a.hazardous)
        normal = next(a for a in arrivals if not (a.heavy or a.hazardous))
        self.assertEqual(self.post({"action": "assign", "arrival": hazmat.id,
                                    "door": "D1", "slot": "09:00"}).status_code, 409)
        self.assertEqual(self.post({"action": "assign", "arrival": heavy.id,
                                    "door": "D3", "slot": "09:00"}).status_code, 409)
        self.assertEqual(self.post({"action": "assign", "arrival": hazmat.id,
                                    "door": "D4", "slot": "09:00"}).status_code, 200)
        taken = self.post({"action": "assign", "arrival": normal.id, "door": "D4", "slot": "09:00"})
        self.assertEqual(taken.status_code, 409)
        self.assertIn("already has", taken.json()["error"])
        self.assertIn(f'data-cell="D4|09:00" data-door="D4" data-slot="09:00"><article', self.board())

    def test_auto_schedule_places_everyone_legally(self):
        from freight import docks
        self.sign_in()
        self.post({"action": "auto"})
        placed = docks.assignments(self.client.session)
        self.assertEqual(len(placed), len(docks.arrivals()))
        arrivals = docks.by_id()
        for arrival_id, (door, _slot) in placed.items():
            self.assertIsNone(docks.problem(arrivals[arrival_id], door))
        self.assertEqual(len({tuple(v) for v in placed.values()}), len(placed), "a cell was double-booked")
        self.post({"action": "clear"})
        self.assertEqual(docks.assignments(self.client.session), {})

    def test_the_no_javascript_form_uses_the_same_rules(self):
        from freight import docks
        self.sign_in()
        hazmat = next(a for a in docks.arrivals() if a.hazardous)
        self.client.post(ROOT + "ops/docks/", {"action": "assign", "arrival": hazmat.id,
                                               "cell": "D2|08:00"})
        self.assertNotIn(hazmat.id, docks.assignments(self.client.session))
        self.client.post(ROOT + "ops/docks/", {"action": "assign", "arrival": hazmat.id,
                                               "cell": "D4|08:00"})
        self.assertEqual(docks.assignments(self.client.session)[hazmat.id], ["D4", "08:00"])

    def test_signed_out_and_bad_requests(self):
        self.assertEqual(self.post({"action": "auto"}).status_code, 401)
        self.sign_in()
        self.assertEqual(self.post({"action": "teleport"}).status_code, 400)


class FleetTests(FreightCase):

    def test_positions_are_a_pure_function_of_time(self):
        from freight import fleet
        a, b = fleet.state("california", now=1_800_000_000), fleet.state("california", now=1_800_000_000)
        self.assertEqual(a, b)
        later = fleet.state("california", now=1_800_000_004)
        moved = [t for t, u in zip(a["trucks"], later["trucks"]) if (t["x"], t["y"]) != (u["x"], u["y"])]
        self.assertTrue(moved, "nothing moved in four seconds")

    def test_the_announced_arrival_happens(self):
        """The id a test waits for is exactly the id the feed shows on arrival."""
        from freight import fleet
        now = 1_800_000_000
        for region in fleet.REGIONS:
            for truck in fleet.state(region, now=now)["trucks"]:
                after = fleet.state(region, now=now + truck["eta_s"] + 1)
                ids = [e["id"] for e in after["events"]]
                self.assertIn(truck["next_arrival"], ids, f"{region} {truck['id']}")

    def test_events_are_recent_and_newest_first(self):
        from freight import fleet
        snap = fleet.state("texas", now=1_800_000_000)
        times = [e["at"] for e in snap["events"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertTrue(all(snap["now"] - 600 < t <= snap["now"] for t in times))
        self.assertLessEqual(len(times), fleet.MAX_EVENTS)

    def test_pages(self):
        self.sign_in()
        self.assertEqual(self.client.get(ROOT + "ops/fleet/?region=northeast").status_code, 200)
        self.assertEqual(self.client.get(ROOT + "ops/fleet/?region=mars").status_code, 200)   # falls back
        self.assertEqual(self.client.get(ROOT + "api/fleet/texas/").json()["region"], "texas")
        self.assertEqual(self.client.get(ROOT + "api/fleet/mars/").status_code, 404)


# --------------------------------------------------------------------------
# 2.15: the mailbox + password reset, shipping labels, the status page,
#       and the accessibility regressions
# --------------------------------------------------------------------------

class PasswordResetTests(FreightCase):

    def request_reset(self, login="driver"):
        resp = self.client.post(ROOT + "login/forgot/", {"login": login})
        self.assertEqual(resp.status_code, 200)
        return re.search(r'id="forgot-ref">([0-9A-F]{6})<', resp.content.decode()).group(1)

    def reset_link(self, ref):
        from freight import mail
        msg = next(m for m in mail.delivered() if f"(ref {ref})" in m.subject)
        return re.search(r"https?://\S+/login/reset/[\w-]+/", msg.text).group(0)

    def use(self, link, password):
        path = link.split("testserver", 1)[1]
        return self.client.post(path, {"password": password, "confirm": password})

    def sign_in_as(self, user, password):
        return self.client.post(ROOT + "login/", {"username": user, "password": password})

    def test_the_whole_flow_and_the_link_works_once(self):
        ref = self.request_reset("driver@acme-freight.example")
        link = self.reset_link(ref)
        self.assertEqual(self.client.get(link.split("testserver", 1)[1])["Referrer-Policy"],
                         "same-origin", "the token in the URL must not leak to other sites "
                         "(and no-referrer would make the browser's own form POST fail CSRF)")
        self.assertEqual(self.use(link, "correct-horse-7").status_code, 302)
        self.assertEqual(self.sign_in_as("driver", "demo-password").status_code, 401)
        self.assertEqual(self.sign_in_as("driver", "correct-horse-7").status_code, 302)
        again = self.client.get(link.split("testserver", 1)[1])
        self.assertEqual(again.status_code, 410)
        self.assertIn(b'data-state="used"', again.content)

    def test_the_reset_form_passes_a_real_browsers_csrf_check(self):
        """Under Referrer-Policy: no-referrer a browser sends `Origin: null`
        with the page's own form POST, and Django's CSRF check refuses it --
        the reset could never be submitted. DEMO-023 found it in a real
        browser; the test client sends no Origin unless told, so say it here."""
        from django.test import Client
        path = self.reset_link(self.request_reset()).split("testserver", 1)[1]
        browser = Client(enforce_csrf_checks=True)
        page = browser.get(path)
        origin = "null" if page["Referrer-Policy"] == "no-referrer" else "http://testserver"
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        resp = browser.post(path, {"csrfmiddlewaretoken": token, "password": "brand-new-pass-1",
                                   "confirm": "brand-new-pass-1"}, HTTP_ORIGIN=origin)
        self.assertEqual(resp.status_code, 302)

    def test_2_1_0_lets_a_link_work_twice(self):
        self.deploy("2.1.0")
        link = self.reset_link(self.request_reset())
        self.assertEqual(self.use(link, "first-new-pass").status_code, 302)
        self.assertEqual(self.use(link, "second-new-pass").status_code, 302, "the planted bug")
        self.assertEqual(self.sign_in_as("driver", "second-new-pass").status_code, 302)

    def test_no_account_enumeration_and_no_mail_for_strangers(self):
        from freight import mail
        known = self.client.post(ROOT + "login/forgot/", {"login": "driver"}).content.decode()
        unknown = self.client.post(ROOT + "login/forgot/", {"login": "nobody"}).content.decode()
        strip = lambda page: re.sub(r'id="forgot-ref">[0-9A-F]{6}<|<strong>\w+</strong>|csrf\S+', "", page)  # noqa: E731
        self.assertEqual(strip(known), strip(unknown))
        self.assertEqual(len(mail.delivered()), 1)

    def test_shared_accounts_and_password_rules(self):
        link = self.reset_link(self.request_reset("tester"))
        page = self.client.get(link.split("testserver", 1)[1]).content.decode()
        self.assertIn('id="reset-shared"', page)
        self.assertEqual(self.use(link, "a-long-new-password").status_code, 400)
        self.assertEqual(self.sign_in_as("tester", "demo-password").status_code, 302,
                         "a shared account's password must never change")
        link = self.reset_link(self.request_reset())
        path = link.split("testserver", 1)[1]
        self.assertEqual(self.client.post(path, {"password": "short", "confirm": "short"}).status_code, 400)
        self.assertEqual(self.client.post(path, {"password": "long-enough-1",
                                                 "confirm": "long-enough-2"}).status_code, 400)
        self.assertEqual(self.client.get(ROOT + "login/reset/not-a-token/").status_code, 404)

    def test_expired_links_are_refused(self):
        from freight import accounts
        link = self.reset_link(self.request_reset())
        token = link.rstrip("/").rsplit("/", 1)[1]
        import time
        self.assertEqual(accounts.reset_state(token, now=time.time() + accounts.RESET_TTL_S + 1)[0],
                         "expired")

    def test_mail_waits_in_the_queue_before_it_is_delivered(self):
        from freight import jobs, mail
        jobs.TIME_SCALE = 1
        mail.send("someone@acme-freight.example", "Hello", "Body", delay_s=30)
        self.assertEqual(mail.delivered(), [])
        import time
        self.assertEqual(len(mail.delivered(now=time.time() + 31)), 1)

    def test_mailbox_pages(self):
        ref = self.request_reset()
        page = self.client.get(ROOT + "mailbox/?to=driver@acme-freight.example").content.decode()
        self.assertIn(f"(ref {ref})", page)
        msg_id = int(re.search(r'<tr data-id="(\d+)"', page).group(1))
        body = self.client.get(ROOT + f"mailbox/{msg_id}/").content.decode()
        self.assertRegex(body, r'<a href="http://testserver/demo/freight/login/reset/[\w-]+/"')
        self.assertEqual(self.client.get(ROOT + "mailbox/999999/").status_code, 404)
        state = self.client.get(ROOT + "api/mailbox/?to=driver@acme-freight.example").json()
        self.assertEqual(state, {"count": 1, "newest": msg_id})
        self.assertEqual(self.client.get(ROOT + "api/mailbox/?to=x@y.z").json()["count"], 0)


class ShippingLabelTests(FreightCase):

    def test_the_label_needs_sign_in_and_404s_unknown_shipments(self):
        self.assertEqual(self.client.get(ROOT + "ops/shipments/AF-100001/label/").status_code, 302)
        self.sign_in()
        self.assertEqual(self.client.get(ROOT + "ops/shipments/AF-1/label/").status_code, 404)
        page = self.client.get(ROOT + "ops/shipments/AF-100001/label/").content.decode()
        self.assertIn('id="label-barcode"', page)
        shipment = self.client.get(ROOT + "ops/shipments/AF-100001/").content.decode()
        self.assertIn('id="print-label"', shipment)
        self.assertIn('target="_blank"', shipment)

    def test_the_svg_barcode_decodes_with_the_scanners_own_decoder(self):
        """The test's decoder (sample_tests/_lib/barcode.py) shares no code
        with the site's encoder -- the two must agree."""
        import sys
        sys.path.insert(0, str(SAMPLES))
        try:
            from _lib import barcode as scanner
        finally:
            sys.path.remove(str(SAMPLES))
        from freight import label
        for text in ("AF-100001", "AF-642042", "Mixed Case ~!{}", " "):
            svg = label.svg(text)
            bars = [(float(x), float(w)) for x, w in
                    re.findall(r'<rect x="([\d.]+)" y="0" width="([\d.]+)" height="64"/>', svg)]
            self.assertEqual(scanner.decode(bars), text)
        widths = label.widths("AF-100001")
        widths[4], widths[5] = widths[5], widths[4]                   # a misprinted symbol
        x, bars = 0, []
        for i, w in enumerate(widths):
            if i % 2 == 0:
                bars.append((x, w))
            x += w
        with self.assertRaises(ValueError):
            scanner.decode(bars)

    def test_the_pdf_is_well_formed(self):
        from freight import label
        self.sign_in()
        resp = self.client.get(ROOT + "ops/shipments/AF-100001/label.pdf")
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment", resp["Content-Disposition"])
        data = resp.content
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertIn(b"(AF-100001) Tj", data)
        # every xref offset must point exactly at its object
        xref = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
        self.assertTrue(data[xref:].startswith(b"xref\n"))
        offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n \n", data)]
        self.assertEqual(len(offsets), 7)
        for number, offset in enumerate(offsets, 1):
            self.assertTrue(data[offset:].startswith(b"%d 0 obj" % number), number)
        stream = re.search(rb"<< /Length (\d+) >>\nstream\n", data)
        length = int(stream.group(1))
        self.assertEqual(data[stream.end() + length:stream.end() + length + 10], b"\nendstream")
        self.assertEqual(label._pdf_text("İstanbul (Ç)"), "(Istanbul \\(Ç\\))")


class StatusPageTests(FreightCase):

    def test_the_status_page_stays_up_and_tells_the_truth(self):
        self.assertIn(b"All systems operational", self.client.get(ROOT + "status/").content)
        releases.set_incident("outage")
        self.assertEqual(self.client.get(ROOT).status_code, 503)
        page = self.client.get(ROOT + "status/")
        self.assertEqual(page.status_code, 200, "a status page must not go down with the site")
        self.assertIn(b'data-state="outage"', page.content)
        data = self.client.get(ROOT + "status.json").json()
        self.assertEqual(data["status"], "outage")
        self.assertEqual({c["status"] for c in data["components"]}, {"outage"})
        self.assertIsNotNone(data["since"])
        releases.set_incident("slow")
        self.assertEqual(self.client.get(ROOT + "status.json").json()["status"], "degraded")
        releases.set_incident(None)
        self.assertEqual(self.client.get(ROOT + "status.json").json()["status"], "operational")

    def test_incidents_and_uptime_from_the_history(self):
        import datetime as dt
        from freight import status
        now = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)
        at = lambda h, m=0: (now - dt.timedelta(hours=h, minutes=m)).isoformat()  # noqa: E731
        history = [
            {"what": "deploy", "version": "1.0.0", "at": at(48)},
            {"what": "outage", "version": "1.0.0", "at": at(10)},
            {"what": "slow", "version": "1.0.0", "at": at(9)},          # ends the outage
            {"what": "clear", "version": "1.0.0", "at": at(8)},
        ]
        s = status.summary(history=history, incident=None, now=now)
        self.assertEqual([(i["kind"], i["minutes"]) for i in s["incidents"]],
                         [("slow", 60), ("outage", 60)])
        self.assertEqual(s["state"], "operational")
        states = [d["state"] for d in s["days"]]
        self.assertEqual(len(states), 30)
        self.assertEqual(states[:27], ["nodata"] * 27, "no invented history before the first event")
        self.assertEqual(states[-1], "outage")
        self.assertAlmostEqual(s["uptime"], 100 * (1 - 3600 / (48 * 3600)), places=2)


class AccessibilityRegressionTests(FreightCase):
    """2.0 and 2.1 break four WCAG rules (the browser test DEMO-022 scans
    them with axe); 1.x and 3.0 must stay clean of all four."""

    def marks(self, version):
        self.deploy(version)
        home = self.client.get(ROOT).content.decode()
        return {
            "lang": re.search(r"<html lang=\"en\">", home) is not None,
            "skip link visible to screen readers": 'class="skip" href="#main" aria-hidden' not in home,
            "map text alternative": 'aria-labelledby="network-title"' in home,
            "accessible brand colours": "brand-bright" not in home,
        }

    def test_the_regressions_come_and_go_with_the_releases(self):
        for version, healthy in (("1.0.0", True), ("1.1.0", True), ("2.0.0", False),
                                 ("2.1.0", False), ("3.0.0", True)):
            marks = self.marks(version)
            self.assertEqual(set(marks.values()), {healthy}, f"{version}: {marks}")

    def test_the_bright_palette_is_the_one_that_fails_contrast(self):
        css = (FREIGHT_DIR / "static" / "freight" / "freight.css").read_text()

        def ratio(fg, bg):
            def lum(hexcolor):
                rgb = [int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
                lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
                return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
            hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        modern = re.search(r"\.theme-modern \{[^}]*--primary: (#[0-9a-f]{6})[^}]*--cta: (#[0-9a-f]{6})", css)
        bright = re.search(r"\.theme-modern\.brand-bright \{ --primary: (#[0-9a-f]{6}); --cta: (#[0-9a-f]{6})", css)
        classic_cta = re.search(r"\.theme-classic \{[^}]*--cta: (#[0-9a-f]{6})", css).group(1)
        for colour in (*modern.groups(), classic_cta):
            self.assertGreaterEqual(ratio("#ffffff", colour), 4.5, colour)
        self.assertGreaterEqual(ratio(modern.group(1), "#f4f5fa"), 4.5, "orange links on the page grey")
        for colour in bright.groups():
            self.assertLess(ratio("#ffffff", colour), 4.5, colour)

    def test_axe_ships_intact_with_its_licence(self):
        import hashlib
        axe = SAMPLES / "_lib" / "axe"
        digest = hashlib.sha256((axe / "axe.min.js").read_bytes()).hexdigest()
        self.assertIn(digest, (axe / "README.md").read_text(), "axe.min.js changed without its README")
        self.assertIn("Mozilla Public License", (axe / "LICENSE").read_text())


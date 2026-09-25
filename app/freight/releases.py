"""Releases of the Acme Freight demo, and which one is deployed.

A test hub earns its keep when the site under test CHANGES. So the demo ships
five releases, each changing something a tester should notice -- and each of
those changes is caught by a different hub feature (the `spoilers`: what
changed, and where the hub shows it). The Release console deploys one; the
hub stamps every run with the deployed version (appconfig target_version),
so the Analytics page ends up telling the story release by release.

The deployed version lives in <data_dir>/freight/release.json, not in
memory: the serving hub AND the terminal's `testhub run` (another process --
in the docker image, another container) must agree on which version a run was
queued against. appconfig reads that same file, before Django is even set up.
"""
import datetime as dt
import json
import os
import tempfile
import threading
from collections import namedtuple

from core import appconfig

Release = namedtuple("Release", "version name date summary notes spoilers flags")

# The knobs a release turns. Everything a version changes goes through one of
# these, so "what differs between 2.0 and 2.1" is answerable from this file.
BASE_FLAGS = {
    "theme": "classic",          # classic | modern -- the 2.0 redesign
    "search_ms": 60,             # simulated database cost per query
    "db_pool": 2,                # simulated connection pool: queries beyond it queue
    "optimizer_step_s": 0.5,     # seconds per route-optimizer iteration
    "manifest_row_s": 0.12,      # seconds per imported manifest row
    "report_unit_s": 1.0,        # seconds per unit of report work
    "customs_step": False,       # quote wizard asks for customs details
    "note_fail_every": 0,        # 3 = every third note save answers 503
    "rotate_session": True,      # new session id at sign-in (fixation defence)
    "csp": True,                 # Content-Security-Policy header
    "frame_protection": True,    # X-Frame-Options / frame-ancestors
    "tracker": False,            # third-party analytics script (an outside host)
    "promo_multiplier": 1,       # 2 = the promo code is applied twice
    "api_eta_field": "eta",      # public API: eta | estimated_delivery | both
    "api_rate_limit": True,      # public API: per-key requests-per-minute limit
    "a11y_regressions": False,   # WCAG failures: no page language, a skip link
                                 # hidden from screen readers, the map without a
                                 # text alternative, hint text too faint to read
    "reset_single_use": True,    # a password-reset link works exactly once
}


def _flags(**changes):
    unknown = set(changes) - set(BASE_FLAGS)
    if unknown:
        raise KeyError(f"unknown release flag(s): {sorted(unknown)}")
    flags = dict(BASE_FLAGS)
    flags.update(changes)
    return flags


_FAST = dict(search_ms=15, db_pool=5, manifest_row_s=0.08)

RELEASES = [
    Release(
        "1.0.0", "Launch", "2026-03-02",
        "The first release: public tracking and quotes, plus the staff portal.",
        ["Track any shipment by its AF- number, on a map",
         "Instant quotes, with promo codes",
         "Staff portal: dashboard, shipment search, route optimizer, "
         "manifest import and monthly reports"],
        [("Nothing is broken -- this is the baseline",
          "Run the Freight regression group once: every test passes, and every "
          "later version is compared against these numbers")],
        _flags()),
    Release(
        "1.1.0", "Faster search", "2026-04-20",
        "Search, tracking and manifest import got a lot faster.",
        ["Shipment search is up to 3x faster (new database index)",
         "Manifest import processes rows about 50% faster"],
        [("Search, tracking and imports really are faster",
          "Analytics speed table and the per-step trend charts: durations drop "
          "for DEMO-010, DEMO-013 and DEMO-015")],
        _flags(**_FAST)),
    Release(
        "2.0.0", "The redesign", "2026-06-15",
        "A brand-new look, four new hubs, and customs details for "
        "international quotes.",
        ["A fresh new design across the whole site",
         "New hubs: Johannesburg, Lagos, Istanbul and Seoul",
         "International quotes now collect customs details up front"],
        [("Every page looks different",
          "DEMO-017 (visual comparison) fails until you re-bless its baseline "
          "-- delete DEMO-017__baseline.png in the tests folder"),
         ("The route optimizer quietly got about 2x slower",
          "Analytics speed table + DEMO-014's 'route optimization' step-timing "
          "trend"),
         ("Saving a note fails every third time (HTTP 503)",
          "DEMO-013 fails some runs but not others: its pass rate drops -- "
          "the flaky-test signal on Metrics and Analytics"),
         ("The public API renamed 'eta' to 'estimated_delivery' -- a breaking "
          "change nobody announced",
          "DEMO-020 (API contract) fails: every client reading 'eta' would break"),
         ("Accessibility went backwards: the pages lose their language, the "
          "skip link is hidden from screen readers, the map loses its text "
          "alternative and the hint text is too faint to read",
          "DEMO-022 (accessibility scan) fails, naming each broken WCAG rule "
          "per page; its full report is saved with the run")],
        _flags(theme="modern", customs_step=True, optimizer_step_s=1.05,
               note_fail_every=3, api_eta_field="estimated_delivery",
               a11y_regressions=True, **_FAST)),
    Release(
        "2.1.0", "Hotfix", "2026-07-08",
        "Fixes the slow optimizer and the note errors from 2.0 -- and adds "
        "usage analytics.",
        ["The route optimizer is fast again",
         "Notes save reliably again",
         "Password-reset emails arrive faster",
         "New: usage analytics on every page"],
        [("Sign-in no longer rotates the session id (session fixation)",
          "DEMO-012's authenticated checks fail; the Security page lists it"),
         ("The Content-Security-Policy and X-Frame-Options headers were dropped",
          "Security page: missing-header findings on every page"),
         ("The 'usage analytics' script calls analytics.acme-metrics.invalid",
          "Security page: external-request, HIGH -- on an air gap that is both "
          "a broken page and a path out of the network"),
         ("Promo codes are applied twice (20% off instead of 10%)",
          "DEMO-011 fails, and its message names both amounts -- the API "
          "quotes (DEMO-020) are wrong the same way"),
         ("The public API's rate limiter was switched off",
          "DEMO-020 lists it with the other API problems: six calls in a row, "
          "no HTTP 429"),
         ("The faster reset emails skipped a step: a password-reset link keeps "
          "working after it has been used",
          "DEMO-023 fails: any old reset email is still a key to the account"),
         ("2.0's accessibility problems are all still there",
          "DEMO-022 still fails")],
        _flags(theme="modern", customs_step=True, rotate_session=False, csp=False,
               frame_protection=False, tracker=True, promo_multiplier=2,
               api_eta_field="estimated_delivery", api_rate_limit=False,
               a11y_regressions=True, reset_single_use=False, **_FAST)),
    Release(
        "3.0.0", "Stable", "2026-09-01",
        "Everything from 2.x fixed, and the fastest Acme Freight yet.",
        ["Security fixes: session handling, headers, no third-party scripts, "
         "single-use reset links",
         "Accessibility fixes: WCAG 2.1 AA across the site again",
         "Correct promo pricing",
         "Fastest route optimizer, search and reports so far"],
        [("Every planted problem is fixed",
          "The whole Freight regression group is green again (after DEMO-017's "
          "baseline was re-blessed for the new design)"),
         ("The optimizer is ~30% faster than 1.0, search the fastest yet",
          "Analytics speed table: 'faster' across the board"),
         ("The API sends BOTH 'eta' and 'estimated_delivery' (eta deprecated)",
          "DEMO-020 green again: old clients keep working, new ones migrate"),
         ("Accessibility and single-use reset links are fixed",
          "DEMO-022 and DEMO-023 green again")],
        _flags(theme="modern", customs_step=True, search_ms=10, db_pool=6,
               manifest_row_s=0.06, optimizer_step_s=0.35, report_unit_s=0.8,
               api_eta_field="both")),
]

FIRST = RELEASES[0]
_lock = threading.Lock()


def by_version(version):
    for release in RELEASES:
        if release.version == version:
            return release
    return None


def deployed():
    """The deployed release; an unknown or garbled state means the first."""
    return by_version(appconfig.get_config().demo_version) or FIRST


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(mutate):
    """Read-modify-write the state file atomically (temp file + rename): a
    run queued mid-deploy reads the old state or the new one, never half a
    file -- and a deploy never loses an incident flag, or the reverse."""
    path = appconfig.get_config().demo_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        state = _read(path)
        mutate(state)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".release-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=1)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _log(state, entry):
    history = [h for h in state.get("history", []) if isinstance(h, dict)][-49:]
    history.append(entry)
    state["history"] = history


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def deploy(version, by="release console"):
    """Make `version` the live release."""
    release = by_version(version)
    if release is None:
        raise ValueError(f"no such release: {version!r}")
    now = _now()

    def mutate(state):
        state["version"] = version
        state["deployed_at"] = now
        _log(state, {"what": "deploy", "version": version, "at": now, "by": by})
    _write_state(mutate)
    return release


# The incident simulator: not a release, a CONDITION -- the kind of thing a
# scheduled smoke run exists to catch at 3 a.m. The Release console (and
# `testhub demo_release --incident`) switch it; every demo page honours it
# except the console itself, so the switch can always be turned back off.
INCIDENTS = {
    "outage": "Outage: every page answers 503 (maintenance)",
    "slow": "Degraded: every page takes 2.5 s longer",
}


def incident():
    """The simulated incident in effect: None, "outage" or "slow"."""
    value = _read(appconfig.get_config().demo_state_path).get("incident")
    return value if value in INCIDENTS else None


def set_incident(kind, by="release console"):
    """Start an incident ("outage" / "slow") or clear it (None)."""
    if kind is not None and kind not in INCIDENTS:
        raise ValueError(f"no such incident: {kind!r} (outage, slow, or clear)")
    now = _now()

    def mutate(state):
        state["incident"] = kind
        _log(state, {"what": kind or "clear", "version": state.get("version") or FIRST.version,
                     "at": now, "by": by})
    _write_state(mutate)


def deploy_history():
    """Newest first: [{"version", "at", "by"}, ...]."""
    path = appconfig.get_config().demo_state_path
    return list(reversed(_read(path).get("history", [])))

"""config.json -- the ONE file that differs between machines.

Search order:
  1. $PW_TESTHUB_CONFIG            (explicit override)
  2. <home>/config.json            where <home> is the parent of the app/
                                   directory: /opt/pw-testhub on a bundle
                                   machine, the repo root in development.

If no file exists, built-in defaults apply (site on 127.0.0.1:8880, target =
the app's own /demo/ page) so a fresh checkout works with zero setup.

Two URLs matter and they are different things:
  site.host / site.port    where THIS automation site listens
  target.url               the website UNDER TEST that Playwright drives
"""
import copy
import json
import os
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent   # .../app
HOME_DIR = APP_DIR.parent                          # /opt/pw-testhub or repo root

DEFAULTS = {
    "site": {
        "host": "127.0.0.1",         # 0.0.0.0 to serve the network / AWS
        "port": 8880,
        "public_url": "",            # full external URL when behind a proxy/ALB
        "url_prefix": "",            # e.g. "/testhub" to share a domain with the app under test
        "behind_proxy": False,       # True behind an ALB/nginx doing HTTPS (X-Forwarded-* trusted)
        "password": "",              # optional shared password gate (empty = open, see docs)
        "allowed_hosts": ["*"],
    },
    "target": {
        "name": "Demo target",
        "url": "",                   # empty -> this app's own /demo/ page
        "version": "0.0.0",          # version of the app under test, recorded on every run
    },
    "paths": {
        "data_dir": "",              # empty -> <home>/data  (tests/, results/, db)
    },
    "runner": {
        "max_parallel": 2,
        "default_timeout_seconds": 300,
        "browser": "chromium",       # chromium | chrome | msedge (channels need the RPMs)
        "headed": False,             # true needs a display (or xvfb-run)
        # The continuous .webm is OFF by default (2.17): it costs more disk than
        # everything else a run keeps, and the activity replay (frames saved
        # only when the page changes) shows the same thing in a fraction of
        # it. Turn it on here, in Settings, or per test ("video": "on").
        "video": False,
        # Passive security observation during every run: response headers,
        # cookie flags, and any request leaving for another host. Costs
        # nothing extra -- the browser is already there.
        "security_checks": True,
        # Hold a systemd inhibitor while anything is running, so the machine
        # cannot suspend through its own overnight schedule.
        "keep_awake": True,
        "trace": True,
        "live_screenshots": True,    # powers the live "watch it run" view
        "activity_frames": True,     # the activity reel: frames saved only when the page paints
        "python": "",                # interpreter for the harness; empty -> this one
    },
    "schedule": {
        "timezone": "America/Los_Angeles",
        "catchup_minutes": 60,       # fire a missed slot if the server comes up within this window
    },
    "maintenance": {
        "retention_days": 0,         # >0: prune run ARTIFACTS older than this at startup (history/DB kept)
    },
    "storage": {
        # Where run artifacts (videos/traces/screenshots/frames) live.
        # "local" = the data directory (the only option on an air-gapped box).
        # "s3"    = upload each finished run to a bucket; the DATABASE always
        #           stays local. Credentials come from the instance role or
        #           the environment -- never from this file.
        "backend": "local",
        "bucket": "",
        "prefix": "testhub",
        "region": "",
        "endpoint_url": "",          # for S3-compatible stores
        "delete_local_after_upload": True,
        "url_expiry_seconds": 3600,
    },
    "debug": False,
}

# The built-in demo's first release -- what it runs until someone deploys
# another in its Release console (freight/releases.py holds the catalog).
DEMO_FIRST_VERSION = "1.0.0"

_lock = threading.Lock()
_config = None


def config_path() -> Path:
    env = os.environ.get("PW_TESTHUB_CONFIG", "").strip()
    if env:
        return Path(env).expanduser()
    return HOME_DIR / "config.json"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


class Config:
    def __init__(self, raw: dict, path: Path):
        self.raw = raw
        self.path = path

    # --- site ---------------------------------------------------------------
    @property
    def site_host(self) -> str:
        return str(self.raw["site"]["host"])

    @property
    def site_port(self) -> int:
        return int(self.raw["site"]["port"])

    @property
    def public_url(self) -> str:
        explicit = str(self.raw["site"].get("public_url") or "").rstrip("/")
        if explicit:
            return explicit
        host = self.site_host if self.site_host not in ("0.0.0.0", "::") else "127.0.0.1"
        return f"http://{host}:{self.site_port}"

    @property
    def url_prefix(self) -> str:
        """Normalised: '' or '/testhub' (leading slash, no trailing)."""
        raw = str(self.raw["site"].get("url_prefix") or "").strip().strip("/")
        return f"/{raw}" if raw else ""

    @property
    def behind_proxy(self) -> bool:
        return bool(self.raw["site"].get("behind_proxy", False))

    @property
    def password(self) -> str:
        """Optional shared-secret gate. Empty = no login (the default;
        rely on the network or an ALB authenticate rule instead)."""
        return str(self.raw["site"].get("password") or "")

    @property
    def app_version(self) -> str:
        path = APP_DIR / "VERSION"
        try:
            return path.read_text().strip()
        except OSError:
            return "unknown"

    @property
    def allowed_hosts(self):
        hosts = self.raw["site"].get("allowed_hosts") or ["*"]
        return list(hosts)

    @property
    def csrf_trusted_origins(self):
        # Same-origin browser posts pass Django's CSRF check without any
        # entry here; this only matters behind a reverse proxy whose public
        # origin differs -- set site.public_url for that case.
        return [self.public_url] if self.public_url else []

    # --- target under test ----------------------------------------------------
    @property
    def target_name(self) -> str:
        return str(self.raw["target"].get("name") or "target")

    @property
    def target_url(self) -> str:
        url = str(self.raw["target"].get("url") or "").strip()
        if url:
            return url
        return f"http://127.0.0.1:{self.site_port}{self.url_prefix}/demo/"

    @property
    def target_is_demo(self) -> bool:
        return not str(self.raw["target"].get("url") or "").strip()

    @property
    def target_version(self) -> str:
        # The built-in demo reports its OWN version: whichever release its
        # Release console deployed (freight/releases.py). target.version in
        # config.json describes a real target; for the demo it could only
        # ever be a stale guess that mislabels every run in Analytics.
        if self.target_is_demo:
            return self.demo_version
        return self.configured_target_version

    @property
    def configured_target_version(self) -> str:
        """target.version exactly as config.json has it (Settings edits this)."""
        return str(self.raw["target"].get("version") or "")

    # --- the built-in demo (Acme Freight) ---------------------------------------
    @property
    def demo_state_path(self) -> Path:
        return self.data_dir / "freight" / "release.json"

    @property
    def demo_version(self) -> str:
        """The deployed demo release. Read from a FILE, not from memory: the
        serving hub and a terminal `testhub run` (another process, in the
        docker image another container) must stamp runs identically. No
        file yet, or an unreadable one -> the first release."""
        try:
            data = json.loads(self.demo_state_path.read_text(encoding="utf-8"))
            version = str(data.get("version") or "").strip()
        except (OSError, ValueError, AttributeError):
            version = ""
        return version or DEMO_FIRST_VERSION

    # --- paths ----------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        raw = str(self.raw["paths"].get("data_dir") or "").strip()
        path = Path(raw).expanduser() if raw else HOME_DIR / "data"
        if not path.is_absolute():
            path = (HOME_DIR / path).resolve()
        return path

    @property
    def tests_dir(self) -> Path:
        return self.data_dir / "tests"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    def ensure_dirs(self):
        for p in (self.data_dir, self.tests_dir, self.results_dir):
            p.mkdir(parents=True, exist_ok=True)

    # --- runner -----------------------------------------------------------------
    @property
    def max_parallel(self) -> int:
        return max(1, int(self.raw["runner"].get("max_parallel", 2)))

    @property
    def default_timeout(self) -> int:
        return max(10, int(self.raw["runner"].get("default_timeout_seconds", 300)))

    @property
    def browser(self) -> str:
        return str(self.raw["runner"].get("browser") or "chromium")

    @property
    def headed(self) -> bool:
        return bool(self.raw["runner"].get("headed", False))

    @property
    def video(self) -> bool:
        return bool(self.raw["runner"].get("video", False))

    @property
    def security_checks(self) -> bool:
        return bool(self.raw["runner"].get("security_checks", True))

    @property
    def keep_awake(self) -> bool:
        return bool(self.raw["runner"].get("keep_awake", True))

    @property
    def trace(self) -> bool:
        return bool(self.raw["runner"].get("trace", True))

    @property
    def live_screenshots(self) -> bool:
        return bool(self.raw["runner"].get("live_screenshots", True))

    @property
    def activity_frames(self) -> bool:
        return bool(self.raw["runner"].get("activity_frames", True))

    @property
    def harness_python(self) -> str:
        return str(self.raw["runner"].get("python") or "").strip()

    # --- schedule ------------------------------------------------------------
    @property
    def timezone(self) -> str:
        return str(self.raw["schedule"].get("timezone") or "America/Los_Angeles")

    @property
    def catchup_minutes(self) -> int:
        return max(0, int(self.raw["schedule"].get("catchup_minutes", 60)))

    # --- artifact storage -----------------------------------------------------
    @property
    def storage_backend(self) -> str:
        return str(self.raw.get("storage", {}).get("backend") or "local").lower()

    @property
    def s3_bucket(self) -> str:
        return str(self.raw.get("storage", {}).get("bucket") or "")

    @property
    def s3_prefix(self) -> str:
        return str(self.raw.get("storage", {}).get("prefix") or "").strip("/")

    @property
    def s3_region(self) -> str:
        return str(self.raw.get("storage", {}).get("region") or "")

    @property
    def s3_endpoint(self) -> str:
        return str(self.raw.get("storage", {}).get("endpoint_url") or "")

    @property
    def s3_delete_local(self) -> bool:
        return bool(self.raw.get("storage", {}).get("delete_local_after_upload", True))

    @property
    def s3_url_expiry(self) -> int:
        return max(60, int(self.raw.get("storage", {}).get("url_expiry_seconds", 3600)))

    @property
    def retention_days(self) -> int:
        return max(0, int(self.raw.get("maintenance", {}).get("retention_days", 0)))

    @property
    def debug(self) -> bool:
        return bool(self.raw.get("debug", False))


def get_config() -> Config:
    global _config
    with _lock:
        if _config is None:
            _config = _load()
        return _config


class ConfigError(Exception):
    """config.json is unusable -- raised with a message a human can act on
    rather than a JSON traceback at import time."""


def _load() -> Config:
    path = config_path()
    raw = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as exc:
            raise ConfigError(
                f"{path} is not valid JSON: {exc}\n"
                f"    Fix the file (a stray comma or missing quote is usual), "
                f"or move it aside to start with defaults.") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")
    cfg = Config(_deep_merge(DEFAULTS, raw), path)
    cfg.ensure_dirs()
    return cfg


def reload_config() -> Config:
    global _config
    with _lock:
        _config = _load()
        return _config


def save_config(updates: dict) -> Config:
    """Deep-merge `updates` into config.json (creating it if absent)."""
    path = config_path()
    on_disk = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
    merged = _deep_merge(on_disk, updates)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return reload_config()

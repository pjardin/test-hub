"""Django settings for the Test Hub.

Everything machine-specific comes from config.json (see core/appconfig.py),
NOT from this file. The same app directory runs unchanged on a laptop, on the
office network, or on AWS -- only config.json differs between machines.
"""
import os
import stat
import secrets
from pathlib import Path

from core import appconfig

CFG = appconfig.get_config()

BASE_DIR = Path(__file__).resolve().parent.parent  # the app/ directory

# --- secret key: generated once per installation, never committed -----------
# Anyone who can read this can forge a session cookie, so it is created
# 0600 from the start (writing then chmod-ing leaves a readable window) and
# re-tightened on every boot -- a restore from a backup, an rsync, or a
# careless cp can easily bring the file back world-readable, and nothing
# would ever have noticed.
_secret_file = CFG.data_dir / ".secret_key"


def _harden(path):
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
    except OSError:
        pass


if _secret_file.exists():
    SECRET_KEY = _secret_file.read_text().strip()
    _harden(_secret_file)
else:
    SECRET_KEY = secrets.token_urlsafe(48)
    _secret_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_secret_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(SECRET_KEY)

# config.json holds site.password when the shared-password gate is on.
if CFG.path.exists() and CFG.raw.get("site", {}).get("password"):
    _harden(CFG.path)


def _harden_data_files(cfg):
    """The database holds django_session rows, and a session_key IS the login
    cookie — a world-readable db.sqlite3 on a shared machine lets any local
    user lift a session and walk past the password gate. Same treatment as
    .secret_key, on every boot: a restore, an rsync or a careless cp brings
    the file back readable and nothing else would ever notice. SQLite gives
    -wal/-shm the db's permissions when it creates them, but hand-copied
    ones are re-tightened too."""
    for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm"):
        _harden(cfg.data_dir / name)


_harden_data_files(CFG)

DEBUG = CFG.debug
ALLOWED_HOSTS = CFG.allowed_hosts

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "freight",      # Acme Freight: the built-in demo site at /demo/freight/
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "core.middleware.EnsureCsrfCookieMiddleware",
    "core.middleware.SharedPasswordMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "testhub.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.views.site_context",
            ],
        },
    },
]

WSGI_APPLICATION = "testhub.wsgi.application"

# SQLite on purpose: zero extra services on an air-gapped machine, and the DB
# is mostly an index -- test definitions live in files, only run history is
# DB-authoritative. The bundled Python links a modern SQLite on both targets.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": CFG.data_dir / "db.sqlite3",
        "OPTIONS": {
            # Runner worker threads and web threads share this file; a busy
            # timeout turns "database is locked" crashes into short waits.
            "timeout": 20,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = CFG.timezone
USE_I18N = False
USE_TZ = True

STATIC_URL = f"{CFG.url_prefix}/static/"
STATIC_ROOT = CFG.data_dir / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The UI posts forms from whatever host the user typed into the browser.
CSRF_TRUSTED_ORIGINS = CFG.csrf_trusted_origins

# Behind an ALB / nginx terminating HTTPS: trust the standard forwarded
# headers so request.scheme/host are the EXTERNAL ones -- without this,
# same-origin CSRF checks fail on every POST over https.
if CFG.behind_proxy:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # 4xx/5xx request noise is not useful in a single-user tool's console.
        "django.request": {"level": "ERROR"},
    },
}

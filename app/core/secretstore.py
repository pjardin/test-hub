"""Credentials for tests that have to log in.

WHY THIS IS SEPARATE FROM EVERYTHING ELSE
Test files travel. They are written on a laptop, exported to a zip, carried
on a disc, imported at work, and committed to git along the way -- that is
the whole design. A password written into a test file, or into the `.json`
sidecar beside it, travels with all of that and ends up somewhere nobody
intended.

So secrets live in ONE file, on ONE machine, at 0600, and are never part of:
  * the test files or their sidecars
  * the tests export/import zip (the disc-transfer format)
  * config.json (which is world-readable on some boxes)

A test refers to a secret by NAME (`ctx.secret("target_password")`). The name
is what travels; the value stays put. Move a test between machines and it
keeps working as soon as that machine has its own value for the name -- which
is also the right behaviour, because the lab password should not be the
laptop password.

THE VALUE MUST NEVER COME BACK OUT
Anything a test does can end up in run.log, an error message or result.json.
`redactor()` builds a filter that replaces every known secret value with
*** wherever text is written, and the harness installs it over stdout and
stderr before the test runs. That is belt and braces on top of "do not print
your password", because people do.
"""
import json
import os
import stat
from pathlib import Path

FILENAME = "secrets.json"
MIN_REDACT_LENGTH = 4       # below this, redaction would mangle ordinary words


def path_for(data_dir: Path) -> Path:
    return Path(data_dir) / FILENAME


def load(data_dir: Path) -> dict:
    """Every secret as {name: value}. Missing or unreadable file = {}."""
    path = path_for(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in data.items()}


def save(data_dir: Path, secrets: dict) -> Path:
    """Write the store 0600, atomically, never leaving a readable window."""
    path = path_for(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # os.open with the mode, rather than write-then-chmod: the latter leaves
    # the file world-readable for however long the write takes.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({str(k): str(v) for k, v in secrets.items()}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    harden(path)
    return path


def harden(path: Path) -> bool:
    """Re-tighten permissions. Returns True if it had to change something."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
            return True
    except OSError:
        pass
    return False


def is_loose(path: Path) -> bool:
    try:
        return path.exists() and bool(stat.S_IMODE(path.stat().st_mode) & 0o077)
    except OSError:
        return False


def names(data_dir: Path) -> list:
    return sorted(load(data_dir).keys())


def redactor(values):
    """A function that removes secret values from any text.

    Longest first, so a secret that contains another secret is replaced whole
    rather than leaving a fragment behind. Values shorter than
    MIN_REDACT_LENGTH are skipped: replacing every occurrence of "ab" would
    shred the logs and hide nothing worth hiding.
    """
    real = sorted({v for v in values if v and len(v) >= MIN_REDACT_LENGTH},
                  key=len, reverse=True)

    def redact(text):
        if not text or not real:
            return text
        out = text
        for value in real:
            if value in out:
                out = out.replace(value, "***")
        return out

    return redact

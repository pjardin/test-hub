"""Playwright codegen ("record a test") integration.

`playwright codegen` opens a real browser window; you click around and it
writes Python for every action. That needs a DISPLAY on the machine running
THIS server, so it is a local/desktop feature: great on the laptop and on a
workstation, unavailable on a headless AWS box (the UI says so instead of
half-working).

The generated script is a standalone `with sync_playwright(): ...` program;
transform_codegen() extracts the actions and re-shapes them into this
project's `def run(page, ctx)` convention.
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from core import appconfig

_lock = threading.Lock()
_session = None  # {"proc": Popen, "out": Path, "started": float, "url": str}


def display_available() -> bool:
    if sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY"))


def start(url: str = "") -> dict:
    global _session
    cfg = appconfig.get_config()
    url = url or cfg.target_url
    with _lock:
        if _session and _session["proc"].poll() is None:
            return {"ok": False, "error": "a recording session is already running"}
        if not display_available():
            return {"ok": False, "error":
                    "no display on the server machine -- the recorder needs a "
                    "desktop session (or run the hub locally to record)"}
        out = Path(tempfile.mkdtemp(prefix="pw-codegen-")) / "recorded.py"
        python = cfg.harness_python or sys.executable
        try:
            proc = subprocess.Popen(
                [python, "-m", "playwright", "codegen",
                 "--target", "python", "-o", str(out), url],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return {"ok": False, "error": f"could not start codegen: {exc}"}
        _session = {"proc": proc, "out": out, "started": time.time(), "url": url}
        return {"ok": True}


def status() -> dict:
    with _lock:
        if _session is None:
            return {"state": "idle"}
        proc = _session["proc"]
        if proc.poll() is None:
            return {"state": "recording",
                    "seconds": int(time.time() - _session["started"])}
        code = ""
        if _session["out"].exists():
            code = transform_codegen(
                _session["out"].read_text(encoding="utf-8"), _session["url"])
        return {"state": "done", "code": code}


def stop() -> dict:
    with _lock:
        if _session is None or _session["proc"].poll() is not None:
            return status_unlocked()
        try:
            _session["proc"].terminate()
        except OSError:
            pass
    # give codegen a moment to flush the output file
    for _ in range(20):
        if _session["proc"].poll() is not None:
            break
        time.sleep(0.25)
    return status()


def status_unlocked():
    if _session is None:
        return {"state": "idle"}
    return {"state": "done", "code": ""}


def clear():
    global _session
    with _lock:
        if _session and _session["proc"].poll() is None:
            try:
                _session["proc"].kill()
            except OSError:
                pass
        _session = None


def transform_codegen(source: str, target_url: str = "") -> str:
    """Best-effort reshape of codegen output into `def run(page, ctx)`.
    Falls back to returning the raw script in a comment block if the format
    is not recognised."""
    lines = source.splitlines()
    start_idx = end_idx = None
    for i, line in enumerate(lines):
        if "context.new_page()" in line:
            start_idx = i + 1
        if start_idx is not None and i > start_idx and (
                "context.close()" in line or line.strip().startswith("# ---")):
            end_idx = i
            break
    if start_idx is None:
        commented = "\n".join("    # " + l for l in lines)
        return ("def run(page, ctx):\n"
                "    # could not auto-convert the recording; original below\n"
                + commented + "\n    raise AssertionError('edit the recorded test')\n")

    body = lines[start_idx:end_idx if end_idx is not None else len(lines)]
    body = [re.sub(r"^    ", "", l) for l in body]  # strip codegen's indent
    if target_url:
        stripped = target_url.rstrip("/")
        body = [l.replace(f'"{target_url}"', "ctx.base_url")
                 .replace(f'"{stripped}"', "ctx.base_url")
                 .replace(f'"{stripped}/"', "ctx.base_url") for l in body]
    body = [l for l in body if l.strip() not in ("browser.close()", "context.close()")]
    indented = "\n".join(("    " + l).rstrip() if l.strip() else "" for l in body)
    return ("def run(page, ctx):\n"
            + (indented or "    pass")
            + "\n\n    ctx.screenshot(\"end\")\n")

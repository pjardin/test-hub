#!/usr/bin/env python3
"""Harness: executes ONE Playwright test file in its own process.

The test file convention is a single function:

    def run(page, ctx):
        page.goto(ctx.base_url)
        assert page.title()

`page` is a normal Playwright sync-API Page. `ctx` carries the target URL,
an artifacts directory, ctx.log() and ctx.screenshot().

The harness owns everything around that call: browser launch, video, trace,
a live JPEG preview for the web UI (CDP screencast -- works for the
chromium-family browsers, silently absent otherwise), per-test timeout via
SIGALRM, a SIGTERM handler so "kill" still produces artifacts, and a
result.json the runner reads back.

Deliberately standalone: no Django imports, conservative Playwright API
surface (works from 1.35 on CentOS 7 through current releases).

Exit codes: 0 passed, 1 failed, 2 harness/browser error, 3 timeout.
"""
import argparse
import importlib.util
import json
import contextlib
import os
import re
import shutil
import signal
import sys
import time
import traceback
from base64 import b64decode
from contextlib import contextmanager
from pathlib import Path


class _TestTimeout(Exception):
    pass


class _Deadline(Exception):
    """A best-effort teardown step ran out of its own time budget."""


_driver_wedged = []


def _try(seconds, label, action):
    """Run `action()`, giving up after `seconds`. Returns its value or None.

    Teardown talks to a Playwright driver that may be wedged: interrupting a
    sync call with an exception (which is how kill and timeout work) leaves
    the driver mid-conversation, and the NEXT call then blocks forever.
    Measured: page.screenshot() hung for the full grace period and the run
    finished with no result.json, no timings and no video.

    One timeout condemns the rest -- a driver that stopped answering does not
    start again, so later steps would each burn their whole budget for
    nothing. Skipping them turns a ~55s wait after pressing Kill into ~10s.

    (Deliberately a function, not a context manager: @contextmanager cannot
    skip its body, so the short-circuit silently ran the very calls it was
    meant to avoid, unbounded.)
    """
    if _driver_wedged:
        print(f"[harness] {label}: skipped (the browser is not responding)",
              flush=True)
        return None

    def _expire(signum, frame):
        raise _Deadline(label)

    previous = signal.signal(signal.SIGALRM, _expire)
    signal.alarm(seconds)
    try:
        return action()
    except _Deadline:
        _driver_wedged.append(label)
        print(f"[harness] {label}: gave up after {seconds}s "
              f"(the browser stopped responding)", flush=True)
    except Exception as exc:
        print(f"[harness] {label}: skipped ({type(exc).__name__})", flush=True)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    return None


class _Killed(Exception):
    pass


class Ctx:
    """What the test gets besides the page."""

    def __init__(self, base_url, artifacts_dir, test_id, secrets=None):
        self.base_url = base_url
        self.artifacts_dir = Path(artifacts_dir)
        self.test_id = test_id
        self._secrets = secrets or {}
        self._security = None          # set by the harness when --security is on
        self.started = time.time()
        self.steps = []
        self.timings = []
        self._page = None
        self._shot_n = 0
        (self.artifacts_dir / "screenshots").mkdir(parents=True, exist_ok=True)

    def secret(self, name, default=None):
        """A credential for this machine, by name.

        The NAME lives in the test file and travels with it; the VALUE lives
        only in this machine's secrets store. That is what lets the same
        login test run on a laptop and in the lab without a password ever
        being written into a file that moves.

        Missing secrets fail loudly: a login test that silently submits an
        empty password fails later, somewhere confusing.
        """
        if name in self._secrets:
            return self._secrets[name]
        if default is not None:
            return default
        raise KeyError(
            f"no secret named {name!r} on this machine. Add it under "
            f"Settings -> Credentials (they are never written into test "
            f"files, so each machine keeps its own).")

    def has_secret(self, name):
        return name in self._secrets

    # -- authenticated security checks -------------------------------------
    # Opt-in, because they only mean anything if the test really performs the
    # flow. Each one is passive: it observes, or repeats a request the
    # application already serves. Nothing here attacks anything.

    @contextlib.contextmanager
    def logging_in(self):
        """Wrap the login steps to check the session is regenerated.

            with ctx.logging_in():
                page.fill("#user", "tester")
                page.fill("#pass", ctx.secret("target_password"))
                page.click("#login")
        """
        observer = self._security
        before = []
        if observer is not None and self._page is not None:
            try:
                before = self._page.context.cookies()
            except Exception:
                before = []
        self.log("logging in")
        try:
            yield
        finally:
            if observer is not None and self._page is not None:
                try:
                    observer.note_login(before, self._page.context.cookies())
                except Exception:
                    pass

    def check_protected_page(self, url=None):
        """Confirm a page that needs a login actually requires one.

        Fetches it twice -- once in a brand new browser context with no
        cookies, once as the logged-in session -- and compares. This is a
        plain GET of a page the application already serves; it is not an
        attack.
        """
        observer = self._security
        if observer is None or self._page is None:
            return
        target = url or self._page.url
        context = self._page.context
        try:
            authed = context.request.get(target, max_redirects=0)
            observer.note_protected_response(
                target, authed.status, authed.headers, authenticated=True)
        except Exception:
            pass
        try:
            browser = context.browser
            fresh = browser.new_context()
            try:
                anon = fresh.request.get(target, max_redirects=0)
                observer.note_protected_response(
                    target, anon.status, anon.headers, authenticated=False)
            finally:
                fresh.close()
        except Exception:
            pass
        self.log(f"checked that {target} requires a session")

    @contextlib.contextmanager
    def logging_out(self):
        """Wrap the logout steps to check the session really dies.

            with ctx.logging_out():
                page.click("#logout")
        """
        observer = self._security
        page = self._page
        saved, url = [], (page.url if page is not None else "")
        if observer is not None and page is not None:
            try:
                saved = page.context.cookies()
            except Exception:
                saved = []
        try:
            yield
        finally:
            if observer is not None and page is not None and saved:
                still_valid = False
                try:
                    fresh = page.context.browser.new_context()
                    try:
                        fresh.add_cookies(saved)
                        response = fresh.request.get(url, max_redirects=0)
                        # Still 200 on the page we were on = the old session
                        # is being accepted after logout.
                        still_valid = 200 <= response.status < 300
                    finally:
                        fresh.close()
                except Exception:
                    still_valid = False
                try:
                    observer.note_logout(still_valid)
                except Exception:
                    pass

    def log(self, message):
        elapsed = time.time() - self.started
        line = f"[{elapsed:7.2f}s] {message}"
        print(f"[test] {line}", flush=True)
        self.steps.append({"t": round(elapsed, 2), "msg": str(message)})

    def screenshot(self, name="shot"):
        if self._page is None:
            return None
        self._shot_n += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)) or "shot"
        path = self.artifacts_dir / "screenshots" / f"{self._shot_n:02d}-{safe}.png"
        try:
            self._page.screenshot(path=str(path))
            self.log(f"screenshot: {path.name}")
        except Exception as exc:  # never let a screenshot kill the test
            self.log(f"screenshot failed: {exc}")
            return None
        return path

    def record_timing(self, op, detail, ms, label=""):
        """One timed event. Auto-called for every page action (see
        TimedPage); tests add their own phases with `with ctx.timed(...)`."""
        name = (label or f"{op} {detail}".strip())[:120]
        if len(self.timings) < 500:
            self.timings.append({"name": name, "op": op,
                                 "detail": str(detail)[:120],
                                 "ms": round(ms, 1),
                                 "t": round(time.time() - self.started, 2)})
        print(f"[timing] {name}: {ms:.0f}ms", flush=True)

    @contextmanager
    def timed(self, label):
        """Time any block of the test:  with ctx.timed(\"login flow\"): ..."""
        t0 = time.time()
        try:
            yield
        finally:
            self.record_timing("block", "", (time.time() - t0) * 1000, label=label)


# Page methods that get automatic timing. Each call becomes one entry in the
# run's timings: name = "click #submit-btn", duration in ms. That is what
# lets the UI compare how long the SAME step takes run after run.
INSTRUMENTED_PAGE_METHODS = frozenset({
    "goto", "reload", "go_back", "go_forward",
    "click", "dblclick", "fill", "type", "press", "check", "uncheck",
    "set_checked", "select_option", "hover", "focus", "tap", "drag_and_drop",
    "set_input_files",
    "wait_for_selector", "wait_for_load_state", "wait_for_url",
    "wait_for_function", "wait_for_timeout", "wait_for_event",
    "evaluate", "inner_text", "text_content", "inner_html", "get_attribute",
    "content", "title", "screenshot",
})


class TimedPage:
    """Transparent wrapper around a Playwright Page: common actions are
    timed into ctx.timings, everything else passes straight through, so
    tests just use `page` normally and timing costs them nothing."""

    def __init__(self, page, ctx):
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_ctx", ctx)

    def __getattr__(self, name):
        # ANY use of the page counts as the test working -- not just the
        # timed subset. Locator-style code (page.locator(...).click(),
        # page.get_by_role(...)) never touches an instrumented Page method,
        # so keying activity off INSTRUMENTED_PAGE_METHODS alone would treat
        # a whole modern test as idle and sample its busy parts at the idle
        # rate. Attribute access is the one thing every style has in common.
        note_page_action()
        attr = getattr(object.__getattribute__(self, "_page"), name)
        if name in INSTRUMENTED_PAGE_METHODS and callable(attr):
            ctx = object.__getattribute__(self, "_ctx")

            def timed_call(*args, **kwargs):
                detail = str(args[0])[:80] if args else ""
                t0 = time.time()
                note_page_action()      # something is happening right now
                try:
                    return attr(*args, **kwargs)
                finally:
                    # ...and again on the way out, so a long blocking wait
                    # counts as idle while it blocks but not after it returns.
                    note_page_action()
                    ctx.record_timing(name, detail, (time.time() - t0) * 1000)

            return timed_call
        return attr

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_page"), name, value)


_REDACT = [lambda text: text]


def _install_redaction(values):
    """Wrap stdout/stderr so a secret value can never be written out.

    Belt and braces on top of "do not print your password". A test can put a
    credential into an assertion message, an exception, a page URL or a log
    line without meaning to, and run.log is kept forever and read by whoever
    opens the run page. Values shorter than 4 characters are left alone --
    replacing every "abc" would shred the log and hide nothing.
    """
    real = sorted({str(v) for v in values if v and len(str(v)) >= 4},
                  key=len, reverse=True)
    if not real:
        return

    def redact(text):
        out = text
        for value in real:
            if value in out:
                out = out.replace(value, "***")
        return out

    _REDACT[0] = redact

    class _Filtered:
        def __init__(self, stream):
            self._stream = stream

        def write(self, text):
            return self._stream.write(redact(text))

        def __getattr__(self, name):
            return getattr(self._stream, name)

    sys.stdout = _Filtered(sys.stdout)
    sys.stderr = _Filtered(sys.stderr)


def _atomic_write_bytes(path: Path, data: bytes):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


FRAME_MIN_INTERVAL = 0.4   # seconds between saved activity frames
FRAME_DECIMATE_AT = 1500   # halve the archive and double the interval here
FRAME_IDLE_AFTER = 10.0    # no page action for this long => nobody is driving
FRAME_IDLE_INTERVAL = 15.0 # ...so sample this sparsely until they are again

# Wall-clock of the last action the TEST issued against the page. The reel
# uses it to tell "actively testing" from "waiting for something".
#
# Content-hash de-duplication cannot carry this on its own: it collapses a
# blinking caret (a few repeating states) but NOT a clock, a progress bar or
# a polling table, where every repaint genuinely differs. A test that starts
# a 30-minute job and waits produced ~918 frames and ~35 MB of reel -- almost
# all of it a countdown ticking to itself. Measured on the demo page's job.
# Starts at import time, not 0.0: an unset stamp reads as "idle since the
# epoch", which would make the reel sample sparsely from the very first frame.
_last_action = [time.time()]

# Callbacks that write out the frame an idle page was last seen in. See
# note_page_action().
_frame_flushers = []


def note_page_action():
    """The test is doing something again.

    Also flushes the reel's held-back frame. While a page is unattended we
    rate-limit hard, which would otherwise throw away the single most
    interesting frame of a long wait: the one showing the thing you were
    waiting FOR. That frame paints while the test is still blocked (the job
    finishes, THEN wait_for_selector returns), so it looks like just another
    idle repaint and gets dropped -- measured: the reel of a 100s job ended
    10.7s before the job completed, missing the completion entirely.
    """
    _last_action[0] = time.time()
    for flush in _frame_flushers:
        try:
            flush()
        except Exception:
            pass


def start_screencast(page, artifacts_dir: Path, live=True, frames=True,
                     started=None):
    """Chromium-family only, via CDP screencast. Two outputs from one stream:

      live.jpg    latest frame, throttled -- the web UI's live view polls it
      frames/     the ACTIVITY REEL: timestamped JPEGs saved when the page
                  painted AND the test was doing something. A test that works
                  for 2 minutes and then waits an hour produces frames for the
                  2 minutes and a handful for the hour. Three things keep it
                  small, in order of what they catch:
                    * Chromium sends no frames at all while nothing changes;
                    * identical frames are dropped (a blinking caret loops
                      between a few states forever);
                    * a page nobody is driving is sampled every
                      FRAME_IDLE_INTERVAL seconds -- which is what handles a
                      clock or progress bar, where every repaint differs and
                      the hash check cannot help.
                  Filenames carry the elapsed milliseconds
                  (f-0000012345.jpg), so no index file is needed and a killed
                  run keeps everything saved so far.

    Long BUSY runs are bounded by decimation: at FRAME_DECIMATE_AT frames,
    every second frame is deleted and the save interval doubles.
    Best-effort by design: any failure just means no preview/reel."""
    state = {"last_live": 0.0, "last_frame": 0.0,
             "interval": FRAME_MIN_INTERVAL, "count": 0,
             "recent_hashes": []}
    live_path = artifacts_dir / "live.jpg"
    frames_dir = artifacts_dir / "frames"
    if frames:
        frames_dir.mkdir(exist_ok=True)
    t0 = started or time.time()

    cdp = page.context.new_cdp_session(page)

    def save_frame(now, data):
        """Write one reel frame. Returns False if it was a duplicate."""
        import hashlib
        digest = hashlib.md5(data).hexdigest()
        if digest in state["recent_hashes"]:
            return False
        state["recent_hashes"] = (state["recent_hashes"] + [digest])[-4:]
        state["last_frame"] = now
        state.pop("pending", None)
        ms = int((now - t0) * 1000)
        (frames_dir / f"f-{ms:010d}.jpg").write_bytes(data)
        state["count"] += 1
        return True

    def flush_pending():
        pend = state.pop("pending", None)
        if pend and frames:
            save_frame(*pend)

    if frames:
        _frame_flushers.append(flush_pending)

    def decimate():
        kept = sorted(frames_dir.glob("f-*.jpg"))
        for stale in kept[::2]:
            try:
                stale.unlink()
            except OSError:
                pass
        state["interval"] *= 2
        state["count"] = len(list(frames_dir.glob("f-*.jpg")))
        print(f"[harness] activity reel decimated to {state['count']} frames "
              f"(interval now {state['interval']:.1f}s)", flush=True)

    def on_frame(params):
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        now = time.time()
        data = None
        if live and now - state["last_live"] >= 0.7:
            state["last_live"] = now
            try:
                data = b64decode(params["data"])
                _atomic_write_bytes(live_path, data)
            except Exception:
                pass
        # Unattended page => the test is waiting, not driving. Sample sparsely
        # regardless of how busily the page repaints; the interesting frames
        # are the ones around what the test actually does. Anything that
        # appears mid-wait is still caught within FRAME_IDLE_INTERVAL.
        idle = (now - _last_action[0]) > FRAME_IDLE_AFTER
        interval = max(state["interval"], FRAME_IDLE_INTERVAL) if idle \
            else state["interval"]
        if frames and idle and now - state["last_frame"] < interval:
            # Hold the newest idle frame rather than discarding it: if the
            # test wakes up next, this is what the page looked like when
            # whatever it was waiting for happened.
            try:
                state["pending"] = (now, data if data is not None
                                    else b64decode(params["data"]))
            except Exception:
                pass
        if frames and now - state["last_frame"] >= interval:
            try:
                if data is None:
                    data = b64decode(params["data"])
                # A blinking caret (or any looping animation between a few
                # states) repaints forever while nothing really happens.
                # Skip frames identical to a recently saved one, so "idle
                # with a cursor in a form field" stays idle in the reel.
                if save_frame(now, data) and state["count"] >= FRAME_DECIMATE_AT:
                    decimate()
            except Exception:
                pass

    cdp.on("Page.screencastFrame", on_frame)
    cdp.send("Page.startScreencast", {
        "format": "jpeg", "quality": 55,
        "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 2,
    })
    return cdp


def load_test_module(path: Path):
    # Tests written as a proper code base -- page-object classes, shared
    # helpers -- import their own modules. Putting the tests folder first on
    # sys.path makes `from _lib.pages import LoginPage` work; the syncer
    # ignores anything starting with "_", so shared code lives in _lib/ (or
    # _helpers.py) without being mistaken for a test.
    tests_dir = str(path.resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    spec = importlib.util.spec_from_file_location("pw_testhub_test", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise RuntimeError(f"{path.name} does not define run(page, ctx)")
    return module


def launch_browser(pw, browser: str, headed: bool):
    headless = not headed
    if browser in ("chrome", "msedge"):
        return pw.chromium.launch(channel=browser, headless=headless)
    if browser == "system-chromium":
        exe = (shutil.which("chromium-browser") or shutil.which("chromium")
               or "/usr/bin/chromium-browser")
        return pw.chromium.launch(executable_path=exe, headless=headless)
    if browser == "firefox":
        # Playwright's own Firefox build; on the offline targets this is not
        # shipped (see BROWSERS.txt) and launch() will say so clearly.
        return pw.firefox.launch(headless=headless)
    return pw.chromium.launch(headless=headless)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-file", required=True)
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--browser", default="chromium")
    ap.add_argument("--test-id", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--live-screenshots", action="store_true")
    ap.add_argument("--activity-frames", action="store_true")
    ap.add_argument("--secrets-file", default="",
                    help="JSON of {name: value}; values are redacted from all output")
    ap.add_argument("--security", action="store_true",
                    help="watch headers/cookies/requests for misconfiguration")
    args = ap.parse_args()

    artifacts = Path(args.artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)

    # Secrets, and the guard that keeps them from coming back out. Installed
    # BEFORE anything else runs: a value that reaches stdout before the filter
    # is in place is already in run.log forever.
    secrets = {}
    if args.secrets_file:
        try:
            with open(args.secrets_file, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                secrets = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, ValueError) as exc:
            print(f"[harness] could not read the credentials store: {exc}",
                  file=sys.stderr, flush=True)
    if secrets:
        _install_redaction(secrets.values())

    ctx = Ctx(args.base_url, artifacts, args.test_id, secrets=secrets)

    result = {
        "status": "error", "error": "", "duration_seconds": None,
        "test_id": args.test_id, "browser": args.browser,
        "base_url": args.base_url, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "steps": ctx.steps,
        "timings": ctx.timings,
    }

    def write_result():
        result["duration_seconds"] = round(time.time() - ctx.started, 3)
        result["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # Redacted here too: result.json is written as bytes and so never
        # passes through the stdout filter, and an exception message is
        # exactly where a mistyped credential ends up.
        payload = _REDACT[0](json.dumps(result, indent=2))
        _atomic_write_bytes(artifacts / "result.json", payload.encode("utf-8"))

    terminating = []

    def on_term(signum, frame):
        # FIRE ONCE. The runner signals on kill, and its watchdog loop may
        # signal again ~2s later; re-raising there landed inside teardown and
        # aborted context.close() -- the call that finalises video.webm. A
        # killed run then kept no video, no result.json and no timings.
        # Repeat signals are ignored; the runner still holds SIGKILL as the
        # backstop if teardown genuinely wedges.
        if terminating:
            return
        terminating.append(True)
        raise _Killed()

    def on_alarm(signum, frame):
        raise _TestTimeout()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(max(5, args.timeout))

    print(f"[harness] test={args.test_id or args.test_file} "
          f"browser={args.browser} headed={args.headed} timeout={args.timeout}s",
          flush=True)
    print(f"[harness] target: {args.base_url}", flush=True)

    exit_code = 2
    try:
        module = load_test_module(Path(args.test_file))
    except SyntaxError as exc:
        # Lead with the ACTIONABLE part. The UI strips everything from
        # "Traceback" onwards, so a bare traceback left the tester looking at
        # "could not load test file:" and nothing else -- no line, no reason.
        where = f"line {exc.lineno}" + (f", column {exc.offset}" if exc.offset else "")
        result["error"] = (
            f"this test file has a Python syntax error at {where}: {exc.msg}.\n"
            f"    {(exc.text or '').strip()}\n"
            f"Fix the file and run it again."
            f"\n\n{traceback.format_exc()}")
        print(result["error"], file=sys.stderr, flush=True)
        write_result()
        sys.exit(2)
    except Exception as exc:
        # RuntimeError here is OUR OWN message (see load_test_module), already
        # written in English -- prefixing it with the class name just leaks an
        # internal identifier at the tester. For anything else the type is the
        # informative part ("ModuleNotFoundError" tells you it is an import).
        first = (str(exc) if isinstance(exc, RuntimeError)
                 else f"{type(exc).__name__}: {exc}").strip().splitlines()[0]
        result["error"] = (
            f"could not load this test file -- {first}.\n"
            f"The file must define  def run(page, ctx)  and import cleanly."
            f"\n\n{traceback.format_exc()}")
        print(result["error"], file=sys.stderr, flush=True)
        write_result()
        sys.exit(2)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        result["error"] = f"playwright is not importable in this interpreter: {exc}"
        print(result["error"], file=sys.stderr, flush=True)
        write_result()
        sys.exit(2)

    browser = context = page = security = None
    try:
        with sync_playwright() as pw:
            try:
                browser = launch_browser(pw, args.browser, args.headed)
                ctx_kwargs = {"viewport": {"width": 1280, "height": 800}}
                if args.video:
                    ctx_kwargs["record_video_dir"] = str(artifacts / "_video_tmp")
                    ctx_kwargs["record_video_size"] = {"width": 1280, "height": 800}
                context = browser.new_context(**ctx_kwargs)
                if args.trace:
                    try:
                        context.tracing.start(screenshots=True, snapshots=True)
                    except Exception as exc:
                        print(f"[harness] tracing unavailable: {exc}", flush=True)
                page = context.new_page()
                ctx._page = page
                if args.security:
                    # Attached BEFORE the test navigates, or the first
                    # document response -- the one whose headers describe the
                    # application -- is already gone.
                    try:
                        from security import SecurityObserver
                    except ImportError:
                        from core.harness.security import SecurityObserver
                    security = SecurityObserver(page, args.base_url).attach()
                    ctx._security = security
            except Exception:
                result["error"] = f"browser launch failed:\n{traceback.format_exc()}"
                print(result["error"], file=sys.stderr, flush=True)
                write_result()
                sys.exit(2)

            if args.live_screenshots or args.activity_frames:
                try:
                    start_screencast(page, artifacts,
                                     live=args.live_screenshots,
                                     frames=args.activity_frames,
                                     started=ctx.started)
                except Exception as exc:
                    print(f"[harness] screen capture unavailable: {exc}", flush=True)

            ctx.log(f"starting {args.test_id or Path(args.test_file).name}")
            note_page_action()      # the run proper begins here
            try:
                module.run(TimedPage(page, ctx), ctx)
                result["status"] = "passed"
                result["error"] = ""
                exit_code = 0
                ctx.log("PASSED")
            except _TestTimeout:
                result["status"] = "timeout"
                # Say what to DO about it: forgetting to raise this is the
                # single most common mistake when writing a test that waits
                # for a long job (the default is 300s, from config.json).
                result["error"] = (
                    f"the test ran longer than its {args.timeout}s time limit "
                    f"and was stopped. If it is meant to take this long, raise "
                    f"\"Timeout seconds\" on the test (Edit test), or "
                    f"runner.default_timeout_seconds in config.json.")
                exit_code = 3
                ctx.log(f"TIMEOUT after {args.timeout}s")
            except _Killed:
                result["status"] = "killed"
                result["error"] = "killed by request"
                exit_code = 2
                ctx.log("KILLED")
            except AssertionError as exc:
                result["status"] = "failed"
                result["error"] = f"AssertionError: {exc}\n{traceback.format_exc()}"
                exit_code = 1
                ctx.log(f"FAILED: {exc}")
            except Exception as exc:
                result["status"] = "failed"
                result["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                exit_code = 1
                ctx.log(f"FAILED: {type(exc).__name__}: {exc}")

            signal.alarm(0)

            # Write the verdict BEFORE touching the browser again. Everything
            # below talks to the Playwright driver, and after a kill that
            # connection is not trustworthy: interrupting a sync call with an
            # exception leaves the driver mid-conversation, so the next call
            # can block forever. Measured: page.screenshot() hung for the
            # full 60s grace and the run finished with no result.json, no
            # timings and no video. Status and timings are the part testers
            # actually need, so they are on disk before any of that risk.
            write_result()

            # Post-mortem artifacts. Each is individually best-effort AND
            # individually time-bounded, so one wedged call cannot eat the
            # budget the others need.
            # Security observations. AFTER write_result() on purpose: this
            # calls page.content(), which is a browser call, and the verdict
            # must already be on disk before anything that can hang. Written
            # as its own artifact (security.json) rather than into result.json
            # so the ordering rule stays simple.
            if security is not None:
                def _security_report():
                    content = ""
                    try:
                        content = page.content()[:200000]
                    except Exception:
                        pass
                    report = security.report(content)
                    _atomic_write_bytes(
                        artifacts / "security.json",
                        json.dumps(report, indent=2).encode("utf-8"))
                    counts = report["counts"]
                    print(f"[harness] security: {counts['high']} high, "
                          f"{counts['medium']} medium, {counts['low']} low",
                          flush=True)

                _try(15, "security report", _security_report)

            def _shot():
                page.screenshot(path=str(artifacts / "failure.png"))
                print("[harness] saved failure.png", flush=True)

            def _trace():
                context.tracing.stop(path=str(artifacts / "trace.zip"))
                print("[harness] saved trace.zip", flush=True)

            def _teardown():
                if context is not None:
                    context.close()      # finalises the video file
                if browser is not None:
                    browser.close()

            if result["status"] != "passed" and page is not None:
                _try(10, "failure.png", _shot)
            if args.trace and context is not None:
                _try(30, "trace.zip", _trace)
            video_src = None
            if args.video and page is not None:
                video_src = _try(10, "video path",
                                 lambda: page.video.path() if page.video else None)
            _try(30, "browser teardown", _teardown)
            if video_src:
                try:
                    src = Path(video_src)
                    if src.exists():
                        os.replace(src, artifacts / "video.webm")
                        print("[harness] saved video.webm", flush=True)
                except Exception:
                    pass
            # Always: a killed or timed-out run never reaches the rename, and
            # the leftover scratch dir holds a full (unwatchable, partial)
            # recording. Left alone it silently doubles disk use per run.
            shutil.rmtree(artifacts / "_video_tmp", ignore_errors=True)
    except (_Killed, _TestTimeout) as exc:
        # Landed outside the inner handlers (e.g. during launch or teardown).
        # These messages are shown to testers, so they must read like English
        # -- never an internal exception class name.
        timed_out = isinstance(exc, _TestTimeout)
        if result["status"] in ("error", "passed"):
            result["status"] = "timeout" if timed_out else "killed"
            result["error"] = result["error"] or (
                f"the test ran longer than its {args.timeout}s time limit "
                "and was stopped while the browser was starting or shutting "
                "down. If it needs longer, raise \"Timeout seconds\" on the "
                "test." if timed_out else
                "stopped by request (Kill) while the browser was starting or "
                "shutting down")
        exit_code = 3 if timed_out else 2
    except Exception:
        result["error"] = f"harness error:\n{traceback.format_exc()}"
        print(result["error"], file=sys.stderr, flush=True)
        exit_code = 2
    finally:
        signal.alarm(0)
        write_result()

    print(f"[harness] result: {result['status']}", flush=True)
    # Everything a tester needs is written by now. Playwright's abandoned
    # futures spew "Future exception was never retrieved / TargetClosedError"
    # tracebacks at interpreter shutdown whenever the browser went away
    # mid-call -- i.e. on every Kill. Testers read run.log and reasonably
    # conclude their test crashed, so drop that trailing noise (and only
    # that: it can only happen after this point).
    try:
        sys.stderr.flush()
        sys.stderr = open(os.devnull, "w")
    except OSError:
        pass
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

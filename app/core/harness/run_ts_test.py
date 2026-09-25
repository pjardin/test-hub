#!/usr/bin/env python3
"""Adapter harness: executes ONE TypeScript test file (@playwright/test)
through the TS engine at /opt/pw-ts, speaking the EXACT contract of
run_test.py -- same argv, same result.json, same exit codes -- so the
runner, the UI, the charts and the kill choreography stay language-blind.

What runs the browser is the node/npm Playwright (the engine's pinned
@playwright/test), not Python Playwright: the spec files are ordinary
`test('...', async ({ page }) => ...)` files.

Feature mapping:
  --video            -> Playwright video (moved to artifacts/video.webm)
  --trace            -> Playwright trace (moved to artifacts/trace.zip)
  --activity-frames  -> the SCREENSHOTS-ON-CHANGE reel: Playwright traces
                        natively record a screencast frame on every paint;
                        this adapter extracts them into frames/f-<ms>.jpg
                        (md5-deduped + decimated), the same files the run
                        page's reel viewer already shows.
  step timings       -> the same traces say when every test.step() and
                        every page/API call began and ended; mapped onto
                        the reel's clock (zero = the test's start, like
                        run_test.py's `started`), so the run page's step
                        list follows the replay for TypeScript runs too.
  --secrets-file     -> values exported as PW_SECRET_<NAME> env vars for
                        the specs, and REDACTED from the streamed log and
                        result.json (no secret ever appears in argv).
  --browser          -> chromium | chrome | msedge (real Chrome/Edge via
                        channels). firefox/webkit fail fast with an honest
                        message: they cannot run on RHEL 8.

Deliberately standalone: no Django imports, stdlib only, Python 3.9.
Exit codes: 0 passed, 1 failed, 2 harness error, 3 timeout.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ENGINE = Path(os.environ.get("PW_TS_ENGINE", "/opt/pw-ts"))
NODE = ENGINE / "node" / "bin" / "node"
PW_CLI = ENGINE / "engine" / "node_modules" / "@playwright" / "test" / "cli.js"
HUB_CONFIG = Path(__file__).resolve().parent / "pw.hub.config.ts"

BROWSER_PROJECTS = {"chromium": "chromium", "chrome": "chrome",
                    "msedge": "msedge", "edge": "msedge"}
MAX_FRAMES = 600
MAX_TIMINGS = 500          # the Python harness's cap too


def _redactor(secret_values):
    pairs = [(v, "*" * 8) for v in secret_values if len(v) >= 4]
    def redact(text):
        for value, mask in pairs:
            text = text.replace(value, mask)
        return text
    return redact


def _atomic_write_bytes(path, payload):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _spec_error_summary(err):
    """One line for one failed expectation: its message (the one the spec
    gave expect(), else the matcher's) plus what a Python assertion message
    would carry -- the locator and expected/received, when the matcher
    states them. Never the code frame or the call log."""
    msg = ""
    if isinstance(err, dict):
        msg = str(err.get("message") or "")
    elif err:
        msg = str(err)
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)          # strip ANSI colors
    lines = [line.strip() for line in msg.strip().split("\n")]
    head = lines[0] if lines else ""
    if head.startswith("Error: "):
        head = head[len("Error: "):]
    found = {}
    for line in lines[1:]:
        if line.startswith("Call log:") or re.match(r"^>?\s*\d+ \|", line):
            break                                        # the log / code frame
        for key in ("Locator:", "Expected:", "Received:"):
            if line.startswith(key) and key not in found:
                found[key] = line[len(key):].strip()[:60]
    extra = []
    if found.get("Locator:") and found["Locator:"] not in head:
        extra.append(found["Locator:"])
    if "Expected:" in found and "Received:" in found:
        extra.append(f"expected {found['Expected:']}, received {found['Received:']}")
    elif "Expected:" in found:
        extra.append(f"expected {found['Expected:']}")
    if extra:
        head += " (" + ", ".join(extra) + ")"
    return head[:400]


def load_and_redact_report(report_path, redact):
    """Read the @playwright/test JSON report and rewrite it REDACTED.

    The report lands in the served artifacts dir and embeds test stdout and
    error text -- a spec that printed a secret would otherwise leak it into
    a downloadable file. Same rule as result.json: nothing unredacted is
    left on disk. Returns the parsed report (or None)."""
    try:
        raw = report_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        _atomic_write_bytes(report_path, redact(raw).encode("utf-8"))
    except OSError:
        pass
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _epoch_ms(iso):
    """'2026-09-25T06:47:00.356Z' (a report's startTime) -> epoch ms."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


def _report_tests(report):
    """[(test title, suite title, last result)] for every test, in order."""
    out = []

    def walk(suites, crumb):
        for suite in suites or []:
            title = suite.get("title") or ""
            deeper = crumb if title in ("", crumb) else title
            for spec in suite.get("specs") or []:
                for t in spec.get("tests") or []:
                    results = t.get("results") or []
                    out.append((spec.get("title", "?"), deeper,
                                results[-1] if results else {}))
            walk(suite.get("suites"), deeper)

    walk((report or {}).get("suites"), "")
    return out


def report_zero_ms(report):
    """When the first test began (epoch ms): the zero of a TypeScript run's
    clock. The reel's frames and the step timings both count from it, as
    run_test.py counts both from its `started`."""
    starts = [_epoch_ms(last.get("startTime")) for _, _, last in _report_tests(report)]
    starts = [s for s in starts if s is not None]
    return min(starts) if starts else None


def _trace_path(result):
    for att in result.get("attachments") or []:
        if att.get("name") == "trace" and att.get("path"):
            return str(att["path"])
    return None


def _camel(expression):
    """'to.be.visible' -> 'toBeVisible': the matcher as the spec wrote it."""
    parts = [p for p in str(expression or "").split(".") if p]
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:]) if parts else ""


def _call_name(cls, method, params):
    """(op, detail) for one library call, in the Python harness's shape:
    `click #login-btn`, `goto freight/`, `toBeVisible #signed-in-user`,
    `GET freight/api/v1/quotes`. WHERE, never WHAT: a fill's value and a
    request's headers and body stay out -- they are passwords and API keys
    as often as not."""
    p = params if isinstance(params, dict) else {}
    if cls == "APIRequestContext" and method == "fetch":
        return str(p.get("method") or "GET").upper(), str(p.get("url") or "")
    if method == "expect":
        return _camel(p.get("expression")) or "expect", str(p.get("selector") or "")
    if method == "waitForEventInfo":
        info = p.get("info") if isinstance(p.get("info"), dict) else {}
        return "waitForEvent", str(info.get("event") or "")
    target = p.get("selector") or p.get("url") or p.get("source") or ""
    return str(method or "?"), str(target)


def read_trace_timeline(path):
    """One test's trace.zip -> {"steps": [(title, start, ms)],
    "actions": [(op, detail, start, ms)]}, starts on the WALL clock (epoch
    ms) -- or None when the trace cannot say (no runner trace before
    Playwright 1.4x; a zip cut short by a kill).

    steps are the test body's test.step() blocks; actions are its page and
    API calls and web-first expects. Both come from the test runner's own
    trace (test.trace), whose monotonic clock its context-options record
    pairs with the wall clock. Fixture and hook work (launching the browser,
    opening the context) is left out, as run_test.py leaves it out. An
    action is NAMED from the library trace (joined by stepId), never from
    the runner's step title: titles carry typed values (Fill "<password>")
    and custom expect messages that read like failures when they passed."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "test.trace" not in names:
                return None
            runner = zf.read("test.trace").decode("utf-8", "replace").splitlines()
            calls = {}
            for name in names:
                if not name.endswith(".trace") or name == "test.trace":
                    continue
                for line in zf.read(name).decode("utf-8", "replace").splitlines():
                    if '"stepId"' not in line or '"before"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "before" and ev.get("stepId"):
                        calls[ev["stepId"]] = (ev.get("class"), ev.get("method"),
                                               ev.get("params"))
    except (OSError, zipfile.BadZipFile, KeyError):
        return None

    anchor = None
    before, ends = [], {}
    for line in runner:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "context-options" and anchor is None:
            try:
                anchor = float(ev["wallTime"]) - float(ev["monotonicTime"])
            except (KeyError, TypeError, ValueError):
                return None
        elif kind == "before" and ev.get("callId"):
            before.append(ev)
        elif kind == "after" and ev.get("callId"):
            ends[ev["callId"]] = ev.get("endTime")
    if anchor is None:
        return None
    category = {ev["callId"]: ev.get("method") for ev in before}
    parent = {ev["callId"]: ev.get("parentId") for ev in before}

    def in_body(call_id):
        seen = set()
        p = parent.get(call_id)
        while p and p not in seen:
            if category.get(p) in ("hook", "fixture"):
                return False
            seen.add(p)
            p = parent.get(p)
        return True

    steps, actions = [], []
    finished = [float(v) for v in ends.values() if isinstance(v, (int, float))]
    for ev in before:
        cid, cat = ev["callId"], ev.get("method")
        if cat in ("hook", "fixture") or not in_body(cid):
            continue
        try:
            start, end = float(ev["startTime"]), float(ends[cid])
        except (KeyError, TypeError, ValueError):
            continue                   # it never finished: a kill cut it short
        at, ms = anchor + start, max(0.0, end - start)
        if cat == "test.step":
            steps.append((str(ev.get("title") or "").strip(), at, ms))
        elif cat in ("pw:api", "expect") and ev.get("stepId") in calls:
            op, detail = _call_name(*calls[ev["stepId"]])
            actions.append((op, detail, at, ms))
    # the test's last moment (its After Hooks closing the context): the
    # report's duration leaves out the worker's browser launch, so the test
    # row is placed by its END
    return {"steps": steps, "actions": actions,
            "end": anchor + max(finished) if finished else None}


def map_report(report, timelines=None):
    """@playwright/test JSON report -> (status, error, timings).

    timings mirror the Python harness's shape ({name, op, detail, ms, t};
    t = when the entry ENDED, in seconds on the run's clock) so the per-step
    trend charts work unchanged: one entry per test case, one per
    test.step() (op "block", like a Python ctx.timed() block) and -- when
    the test's trace was read (`timelines`: trace path ->
    read_trace_timeline) -- one per page/API call and web-first expect, as
    run_test.py times every page action.

    Entries whose start is really known carry "start" (seconds); the run
    page follows the replay with a TypeScript run's list only when every
    entry does. Without a timeline the report is all there is, and it gives
    test.step() blocks a duration but no start: they are laid end to end
    from their test's start -- listed, not synced.
    """
    timings = []
    failures = []
    zero = report_zero_ms(report)
    cursor = 0.0                    # the fallback clock: tests end to end
    tests = _report_tests(report)
    for title, crumb, last in tests:
        status = last.get("status", "unknown")
        ms = float(last.get("duration") or 0)
        begun = _epoch_ms(last.get("startTime"))
        known = begun is not None and zero is not None
        start = (begun - zero) / 1000.0 if known else cursor
        timeline = (timelines or {}).get(_trace_path(last)) if known else None
        if timeline and timeline.get("end"):
            # Playwright's duration leaves out the browser launch (a worker
            # fixture): the test ran for `ms` up to the trace's last moment
            start = max(0.0, (timeline["end"] - zero) / 1000.0 - ms / 1000.0)
        entry = {"name": title[:120], "op": "test", "detail": crumb[:120],
                 "ms": round(ms, 1), "t": round(start + ms / 1000.0, 2)}
        if known:
            entry["start"] = round(start, 3)
        timings.append(entry)
        if timeline:
            for name, at, sms in timeline["steps"]:
                s = (at - zero) / 1000.0
                timings.append({"name": name[:120], "op": "block",
                                "detail": title[:120], "ms": round(sms, 1),
                                "t": round(s + sms / 1000.0, 2), "start": round(s, 3)})
            for op, detail, at, sms in timeline["actions"]:
                s = (at - zero) / 1000.0
                timings.append({"name": f"{op} {detail[:80]}".strip()[:120], "op": op,
                                "detail": detail[:120], "ms": round(sms, 1),
                                "t": round(s + sms / 1000.0, 2), "start": round(s, 3)})
        else:
            # the report's own steps: the top-level test.step() blocks (the
            # JSON reporter lists no others), each with a duration only
            s = start
            for step in last.get("steps") or []:
                name = str(step.get("title", "")).strip()
                if not name or name in ("Before Hooks", "After Hooks"):
                    continue
                sms = float(step.get("duration") or 0)
                timings.append({"name": name[:120], "op": "block",
                                "detail": title[:120], "ms": round(sms, 1),
                                "t": round(s + sms / 1000.0, 2)})
                s += sms / 1000.0
        cursor = start + ms / 1000.0
        if status not in ("passed", "skipped"):
            # every failed expectation: expect.soft() goes on after one, so
            # a run can hold several -- say them all, as DEMO-020's list does
            errors = last.get("errors") or ([last["error"]] if last.get("error") else [])
            problems = [p for p in (_spec_error_summary(e) for e in errors) if p]
            failures.append((title, problems))

    timings = timings[:MAX_TIMINGS]
    total = len(tests)
    if total == 0:
        return "failed", "the spec file contains no tests", timings
    if failures:
        title, problems = failures[0]
        error = (f"{len(failures)} of {total} test(s) failed: "
                 + "; ".join(t for t, _ in failures[:3]))
        if len(problems) == 1:
            error += f" -- {problems[0]}"
        elif problems:
            error += (f" -- {len(problems)} problems:\n  - "
                      + "\n  - ".join(problems[:6]))
        return "failed", error, timings
    return "passed", "", timings


def extract_reel(trace_zips, frames_dir, zero_ms=None):
    """Screenshots-on-change from Playwright traces: traces record a
    screencast frame whenever the page paints; md5-dedupe drops the frames
    where nothing actually differs (a blinking caret repaints forever) and
    decimation caps pathological cases. Filenames carry elapsed ms in the
    Python reel's exact format, so the existing viewer needs nothing.

    Returns (frames written, on_run_clock). With `zero_ms` (the test's
    start, epoch ms) frames are named by time since then -- the clock the
    step timings use, mapped through each trace's context-options record
    -- and on_run_clock is True. When a trace cannot say, frames count from
    the first one instead, and the step list must not claim to follow them.
    """
    frames = []   # (monotonic ms, wall ms or None, sha1, zip_path)
    for zp in trace_zips:
        try:
            with zipfile.ZipFile(zp) as zf:
                for name in zf.namelist():
                    if not name.endswith(".trace"):
                        continue
                    anchor = None
                    for line in zf.read(name).decode("utf-8", "replace").splitlines():
                        if anchor is None and '"context-options"' in line:
                            try:
                                ev = json.loads(line)
                                anchor = float(ev["wallTime"]) - float(ev["monotonicTime"])
                            except (ValueError, KeyError, TypeError):
                                pass
                            continue
                        if '"screencast-frame"' not in line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("type") == "screencast-frame" and ev.get("sha1"):
                            ts = float(ev.get("timestamp") or 0)
                            wall = (anchor + ts if anchor is not None
                                    else ev.get("frameSwapWallTime"))
                            frames.append((ts, wall, ev["sha1"], zp))
        except (OSError, zipfile.BadZipFile):
            continue
    if not frames:
        return 0, True
    on_clock = zero_ms is not None and all(f[1] is not None for f in frames)
    if on_clock:
        frames.sort(key=lambda f: f[1])
        key, base = 1, zero_ms
    else:
        frames.sort(key=lambda f: f[0])
        key, base = 0, frames[0][0]
    step = max(1, (len(frames) + MAX_FRAMES - 1) // MAX_FRAMES)
    frames_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    last_md5 = None
    zips = {}
    try:
        for i, frame in enumerate(frames):
            if i % step:
                continue
            sha1, zp = frame[2], frame[3]
            if zp not in zips:
                zf = zipfile.ZipFile(zp)
                zips[zp] = (zf, set(zf.namelist()))
            zf, members = zips[zp]
            member = f"resources/{sha1}"
            if member not in members:
                member = f"resources/{sha1}.jpeg"
                if member not in members:
                    continue
            data = zf.read(member)
            digest = hashlib.md5(data).hexdigest()
            if digest == last_md5:
                continue
            last_md5 = digest
            ms = max(0, int(float(frame[key]) - base))
            (frames_dir / f"f-{ms:010d}.jpg").write_bytes(data)
            written += 1
    finally:
        for zf, _ in zips.values():
            zf.close()
    return written, on_clock


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
    ap.add_argument("--live-screenshots", action="store_true")   # accepted; reel covers it
    ap.add_argument("--activity-frames", action="store_true")
    ap.add_argument("--secrets-file", default="")
    ap.add_argument("--security", action="store_true")           # accepted; python-only
    args = ap.parse_args()

    artifacts = Path(args.artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.time()

    result = {
        "status": "error", "error": "", "duration_seconds": None,
        "test_id": args.test_id, "browser": args.browser,
        "base_url": args.base_url,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "typescript (@playwright/test via /opt/pw-ts)",
        "steps": [], "timings": [],
    }

    secrets = {}
    if args.secrets_file:
        try:
            with open(args.secrets_file, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                secrets = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, ValueError) as exc:
            print(f"[ts-harness] could not read the credentials store: {exc}",
                  flush=True)
    redact = _redactor(secrets.values())

    def write_result():
        # the duration is the TEST's: a later rewrite (step times refined
        # from the traces) must not add the artifact work to it
        if result["duration_seconds"] is None:
            result["duration_seconds"] = round(time.time() - started, 3)
            result["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = redact(json.dumps(result, indent=2))
        _atomic_write_bytes(artifacts / "result.json", payload.encode("utf-8"))

    # --- preconditions, stated in tester English ----------------------------
    if not NODE.exists() or not PW_CLI.exists():
        result["error"] = (
            "this is a TypeScript test, and the TS engine is not installed "
            f"(expected {ENGINE}). Install the TS engine bundle "
            "(pw-ts-bundle-*.zip) and run again.")
        write_result()
        print(f"[ts-harness] {result['error']}", flush=True)
        return 2
    project = BROWSER_PROJECTS.get(args.browser)
    if not project:
        result["error"] = (
            f"browser '{args.browser}' is not available for TypeScript tests: "
            "Playwright's firefox/webkit builds cannot run on RHEL 8. "
            "Use chromium, chrome or msedge.")
        write_result()
        print(f"[ts-harness] {result['error']}", flush=True)
        return 2

    tests_dir = Path(args.test_file).resolve().parent
    # imports inside specs resolve from the engine (idempotent convenience;
    # the installer also creates this)
    nm_link = tests_dir / "node_modules"
    if not nm_link.exists():
        try:
            nm_link.symlink_to(ENGINE / "engine" / "node_modules")
        except OSError:
            pass

    want_trace_user = bool(args.trace)
    want_frames = bool(args.activity_frames)
    output_dir = artifacts / "test-results"
    report_path = artifacts / "pw-report.json"

    env = os.environ.copy()
    env.update({
        "PLAYWRIGHT_BROWSERS_PATH": str(ENGINE / "browsers"),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report_path),
        "NODE_PATH": str(ENGINE / "engine" / "node_modules"),
        "PW_TS_TESTS_DIR": str(tests_dir),
        "PW_TS_BASE_URL": args.base_url,
        "PW_TS_TIMEOUT_MS": str(max(30, args.timeout) * 1000),
        "PW_TS_VIDEO": "1" if args.video else "",
        # traces are forced on when the reel is wanted: they are where the
        # paint-triggered screenshots live
        "PW_TS_TRACE": ("full" if want_trace_user
                        else ("frames" if want_frames else "")),
        "PW_TS_HEADED": "1" if args.headed else "",
    })
    for key, value in secrets.items():
        safe = re.sub(r"[^A-Za-z0-9_]", "_", key.upper())
        env[f"PW_SECRET_{safe}"] = value

    cmd = [str(NODE), str(PW_CLI), "test", Path(args.test_file).name,
           "--config", str(HUB_CONFIG),
           f"--project={project}",
           f"--output={output_dir}",
           "--reporter=list,json"]
    if args.headed:
        cmd.append("--headed")

    print(f"[ts-harness] @playwright/test project={project} "
          f"file={Path(args.test_file).name}", flush=True)

    child = subprocess.Popen(
        cmd, cwd=str(tests_dir), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    state = {"killed": False, "timed_out": False, "handled": False}

    def stop_child(reason):
        try:
            child.terminate()
        except OSError:
            return
        deadline = time.time() + 15
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.25)
        if child.poll() is None:
            print(f"[ts-harness] {reason}: runner ignored SIGTERM, killing",
                  flush=True)
            try:
                child.kill()
            except OSError:
                pass

    def on_term(signum, frame):
        # FIRE ONCE (the runner's watchdog may signal again ~2s later)
        if state["handled"]:
            return
        state["handled"] = True
        state["killed"] = True
        print("[ts-harness] kill requested -- stopping the TypeScript runner",
              flush=True)
        stop_child("kill")

    def on_alarm(signum, frame):
        if state["handled"]:
            return
        state["handled"] = True
        state["timed_out"] = True
        print(f"[ts-harness] timeout after {args.timeout}s -- stopping",
              flush=True)
        stop_child("timeout")

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(max(1, args.timeout))

    assert child.stdout is not None
    for raw in iter(child.stdout.readline, b""):
        line = redact(raw.decode("utf-8", "replace"))
        sys.stdout.write(line)
        sys.stdout.flush()
    child.wait()
    signal.alarm(0)

    # --- verdict FIRST (the durability rule), artifacts after ---------------
    report = load_and_redact_report(report_path, redact)

    if report is not None:
        status, error, timings = map_report(report)
        result["status"] = status
        result["error"] = error
        result["timings"] = timings
    if state["killed"]:
        result["status"] = "killed"
        result["error"] = result["error"] or "stopped on request"
    elif state["timed_out"]:
        result["status"] = "timeout"
        result["error"] = (f"timed out after {args.timeout}s and was stopped "
                           "(raise timeout_seconds in the sidecar if the test "
                           "legitimately needs longer)")
    elif report is None:
        result["status"] = "error"
        result["error"] = ("the TypeScript runner produced no report -- "
                           "usually a compile error in the spec; the log above "
                           "has the exact line")
    write_result()

    # --- artifacts: video / trace / the screenshots-on-change reel ----------
    def newest(pattern):
        found = sorted(output_dir.rglob(pattern),
                       key=lambda p: p.stat().st_mtime if p.exists() else 0)
        return found[-1] if found else None

    if args.video:
        vid = newest("*.webm")
        if vid:
            shutil.move(str(vid), str(artifacts / "video.webm"))
    trace_zips = sorted(output_dir.rglob("trace.zip"))
    zero = report_zero_ms(report) if report is not None else None
    on_clock = True
    if want_frames and trace_zips:
        n, on_clock = extract_reel(trace_zips, artifacts / "frames", zero)
        print(f"[ts-harness] activity reel: {n} change-frames extracted "
              "from the trace screencast", flush=True)
    # every step and page call, on the reel's clock -- the traces know when
    # each began; read them before they are moved or deleted below
    if report is not None and trace_zips and on_clock:
        timelines = {}
        for _, _, last in _report_tests(report):
            path = _trace_path(last)
            if path and path not in timelines:
                timelines[path] = read_trace_timeline(path)
        if any(timelines.values()):
            result["timings"] = [dict(t, name=redact(t["name"]), detail=redact(t["detail"]))
                                 for t in map_report(report, timelines)[2]]
            write_result()
    if want_trace_user and trace_zips:
        shutil.move(str(trace_zips[-1]), str(artifacts / "trace.zip"))
    elif trace_zips:
        for zp in trace_zips:   # traces existed only to feed the reel
            try:
                zp.unlink()
            except OSError:
                pass

    return {"passed": 0, "failed": 1, "killed": 1,
            "timeout": 3}.get(result["status"], 2)


if __name__ == "__main__":
    sys.exit(main())

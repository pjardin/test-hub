"""Remote Recorder: record a test THROUGH the hub, from any plain browser.

Why it exists: on a deployed hub (AWS, office server) there is no display,
so `playwright codegen` cannot open a window for you -- and the workstation
browser you use to reach the hub may be locked down (no DevTools, no
extensions). This recorder needs none of that. The flow:

    browser tab (plain <img> + fetch)  <-- JPEG frames --   hub server
                                       --  mouse/keys  -->  bundled Chromium
                                                            (drives the TARGET)

  * The hub launches its own (headless) Chromium at the target URL.
  * CDP screencast frames stream to the recorder tab; your clicks and
    keystrokes are posted back and injected via CDP Input events.
  * An init-script inside the page captures the SEMANTIC action for every
    interaction (which element, what selector, what value) through an
    exposed binding -- so the output is `page.fill('#user', ...)`, not
    brittle x/y coordinates.
  * Stop turns the step list into `def run(page, ctx)` code.

Threading: the sync Playwright API is single-threaded, so one session
thread owns ALL Playwright/CDP calls. Web threads only push input commands
onto a queue and read plain-Python state (frames, steps) under a lock. The
session thread pumps events with page.wait_for_timeout, which is what lets
screencast frames and binding callbacks arrive.

Chromium-family only (CDP) -- which is every browser this hub can drive.
"""
import json
import logging
import queue
import re
import threading
import time

from core import appconfig

log = logging.getLogger("testhub.remote_recorder")

VIEWPORT = {"width": 1280, "height": 800}
IDLE_TIMEOUT = 600  # seconds without input before the session self-closes

# Installed into every page of the recording context. Reports one JSON step
# per user interaction through the __pwhub_record binding.
CAPTURE_SCRIPT = r"""
(() => {
  if (window.__pwhubInstalled) return;
  window.__pwhubInstalled = true;
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s);
  const q = (s) => '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';

  function selectorFor(el) {
    if (!el || el.nodeType !== 1) return "body";
    if (el.dataset && el.dataset.testid) return '[data-testid=' + q(el.dataset.testid) + ']';
    if (el.id) return '#' + esc(el.id);
    const tag = el.tagName.toLowerCase();
    if (el.name && (tag === 'input' || tag === 'select' || tag === 'textarea'))
      return tag + '[name=' + q(el.name) + ']';
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return '[aria-label=' + q(aria) + ']';
    if (tag === 'button' || tag === 'a' || el.getAttribute('role') === 'button') {
      const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50);
      if (text) return tag + ':has-text(' + q(text) + ')';
    }
    // short structural fallback
    const path = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && path.length < 5) {
      if (cur.id) { path.unshift('#' + esc(cur.id)); break; }
      let piece = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (same.length > 1) piece += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      }
      path.unshift(piece);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  }

  window.__pwhubSelectorFor = selectorFor;
  const report = (step) => {
    try { window.__pwhub_record(JSON.stringify(step)); } catch (e) {}
  };
  const TYPABLE = ['text', 'email', 'password', 'search', 'tel', 'url', 'number', ''];

  document.addEventListener('pointerdown', (e) => {
    const el = (e.target.closest &&
                e.target.closest('button,a,input,select,textarea,label,[role="button"],[onclick]'))
               || e.target;
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'select') return;                       // change event covers it
    if ((tag === 'input') && (el.type === 'checkbox' || el.type === 'radio')) return;
    if ((tag === 'input' && TYPABLE.includes(el.type || '')) || tag === 'textarea')
      return;                                           // fill covers it
    report({ action: 'click', selector: selectorFor(el) });
  }, true);

  document.addEventListener('input', (e) => {
    const el = e.target, tag = (el.tagName || '').toLowerCase();
    if (tag === 'textarea' || (tag === 'input' && TYPABLE.includes(el.type || '')))
      report({ action: 'fill', selector: selectorFor(el), value: el.value });
  }, true);

  document.addEventListener('change', (e) => {
    const el = e.target, tag = (el.tagName || '').toLowerCase();
    if (tag === 'select')
      report({ action: 'select', selector: selectorFor(el), value: el.value });
    else if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio'))
      report({ action: 'click', selector: selectorFor(el) });
  }, true);

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const el = document.activeElement;
    if (el && ['input', 'textarea'].includes((el.tagName || '').toLowerCase()))
      report({ action: 'press', selector: selectorFor(el), value: 'Enter' });
  }, true);
})();
"""


PRIOR_STEP_RE = re.compile(r"^\s*((?:page|ctx)\.\w+\(.*|with ctx\.timed\(.*)$")


def extract_prior_steps(code: str) -> list:
    """Human-readable list of the page/ctx actions already in a test's code,
    so a continuation recording shows where you left off."""
    out = []
    for line in (code or "").splitlines():
        match = PRIOR_STEP_RE.match(line)
        if match:
            out.append(match.group(1).strip()[:110])
    return out[:60]


_START_RE = re.compile(r"[A-Za-z0-9._~/?=&%+-]*")


def clean_start_path(path) -> str:
    """Where a recording BEGINS, as a path under the target ("freight/"):
    relative, no scheme or host, no "..". The session stays on the site
    under test, and the generated code replays it by adding the same path
    to the test's base URL."""
    path = str(path or "").strip().lstrip("/")
    if not path:
        return ""
    if ("//" in path or ".." in path.split("/") or len(path) > 200
            or not _START_RE.fullmatch(path)):
        raise ValueError("start must be a page under the target, e.g. freight/")
    return path


def goto_line_py(start_path=""):
    if start_path:
        return f'page.goto(ctx.base_url + "{start_path}")'
    return "page.goto(ctx.base_url)"


def goto_line_ts(start_path=""):
    # './' and never '/': baseURL is target.url, and when the target lives
    # under a path -- the built-in demo is at /demo/ -- '/' is the SERVER
    # root (the hub's own dashboard), not the site under test.
    return f"await page.goto('{start_path or './'}');"


class RecorderSession:
    def __init__(self, target_url, test_id="", prior_steps=None, start_path=""):
        self.target_url = target_url
        self.start_path = start_path
        self.start_url = target_url + start_path
        self.test_id = test_id
        self.prior_steps = prior_steps or []
        self.commands = queue.Queue()
        self.lock = threading.Lock()
        self.frame = b""
        self.frame_seq = 0
        self.steps = []
        self.inspect_seq = 0
        self.inspect_result = None
        self.state = "starting"   # starting | recording | done | error
        self.error = ""
        self.code = ""
        self.code_ts = ""
        self.started = time.time()
        self.last_input = time.time()
        self.stop_requested = False
        self._page_ref = None
        self.thread = threading.Thread(target=self._run, name="remote-recorder",
                                       daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------ web side
    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "error": self.error,
                "seq": self.frame_seq,
                "steps": list(self.steps[-40:]),
                "step_count": len(self.steps),
                "viewport": VIEWPORT,
                "seconds": int(time.time() - self.started),
                "test_id": self.test_id,
                "prior_steps": self.prior_steps,
                "inspect_seq": self.inspect_seq,
                "inspect": self.inspect_result,
            }

    MANUAL_ACTIONS = frozenset({
        "assert_text", "assert_value", "assert_visible", "assert_css",
        "wait_visible", "wait_hidden", "wait_text",
    })

    def add_manual_step(self, step: dict) -> bool:
        """A check/wait chosen from the right-click palette (web thread)."""
        action = str(step.get("action", ""))
        if action not in self.MANUAL_ACTIONS:
            return False
        clean = {"action": action,
                 "selector": str(step.get("selector", ""))[:300],
                 "value": str(step.get("value", ""))[:500],
                 "extra": str(step.get("extra", ""))[:100]}
        try:
            timeout = int(step.get("timeout_ms") or 0)
        except (TypeError, ValueError):
            timeout = 0
        if timeout:
            clean["timeout_ms"] = min(timeout, 4 * 3600 * 1000)
        with self.lock:
            self.steps.append(clean)
        return True

    def get_frame(self):
        with self.lock:
            return self.frame, self.frame_seq

    def send_input(self, event: dict):
        self.last_input = time.time()
        self.commands.put(("input", event))

    def stop(self):
        self.stop_requested = True

    # -------------------------------------------------------------- session thread
    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            with self.lock:
                self.state, self.error = "error", f"playwright not importable: {exc}"
            return
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(viewport=VIEWPORT)
                context.add_init_script(CAPTURE_SCRIPT)
                context.expose_binding("__pwhub_record", self._on_step)
                page = context.new_page()
                self._page_ref = page

                cdp = context.new_cdp_session(page)
                cdp.on("Page.screencastFrame", lambda p: self._on_frame(cdp, p))
                cdp.send("Page.startScreencast", {
                    "format": "jpeg", "quality": 60,
                    "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
                    "everyNthFrame": 1,
                })

                page.goto(self.start_url, wait_until="domcontentloaded")
                with self.lock:
                    self.state = "recording"
                log.info("remote recorder started at %s", self.start_url)

                while not self.stop_requested:
                    if time.time() - self.last_input > IDLE_TIMEOUT:
                        log.info("remote recorder idle timeout")
                        break
                    self._drain_commands(cdp)
                    # pumps the event loop: screencast frames + binding calls
                    page.wait_for_timeout(30)

                try:
                    cdp.send("Page.stopScreencast")
                except Exception:
                    pass
                context.close()
                browser.close()
        except Exception as exc:
            # A recording is minutes of someone's work. Even if the browser
            # died (a target="_blank" link, the app calling window.close(),
            # a driver hiccup), the steps captured so far are in memory --
            # turn them into code rather than discarding them.
            log.exception("remote recorder session failed")
            with self.lock:
                self.error = str(exc)[:500]
                self.code = build_code(self.steps, self.target_url, self.start_path)
                self.code_ts = build_code_ts(self.steps, self.target_url, self.start_path)
                self.state = "done" if self.steps else "error"
            return
        with self.lock:
            self.code = build_code(self.steps, self.target_url, self.start_path)
            self.code_ts = build_code_ts(self.steps, self.target_url, self.start_path)
            self.state = "done"
        log.info("remote recorder finished: %d step(s)", len(self.steps))

    def _drain_commands(self, cdp):
        while True:
            try:
                kind, event = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "input":
                    self._dispatch(cdp, event)
                elif kind == "inspect":
                    self._inspect(event)
            except Exception as exc:  # one bad event must not kill the session
                log.debug("%s dispatch failed: %s", kind, exc)

    INSPECT_JS = """
    ([x, y]) => {
      const el = document.elementFromPoint(x, y);
      if (!el) return null;
      const sel = (window.__pwhubSelectorFor || (() => 'body'))(el);
      const cs = getComputedStyle(el);
      const prev = el.style.outline;
      el.style.outline = '3px solid #2a78d6';
      setTimeout(() => { el.style.outline = prev; }, 1600);
      const tag = el.tagName.toLowerCase();
      return {
        selector: sel,
        tag: tag,
        text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
        value: ('value' in el && ['input','textarea','select'].includes(tag)) ? String(el.value).slice(0, 200) : null,
        color: cs.color,
        background: cs.backgroundColor,
        visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
      };
    }"""

    def _inspect(self, event):
        page = self._page_ref
        if page is None:
            return
        try:
            result = page.evaluate(self.INSPECT_JS,
                                   [float(event.get("x", 0)), float(event.get("y", 0))])
        except Exception as exc:
            result = {"error": str(exc)[:200]}
        with self.lock:
            self.inspect_result = result
            self.inspect_seq += 1

    def request_inspect(self, x, y):
        self.last_input = time.time()
        self.commands.put(("inspect", {"x": x, "y": y}))

    @staticmethod
    def _dispatch(cdp, ev):
        etype = ev.get("type")
        x = float(ev.get("x", 0))
        y = float(ev.get("y", 0))
        if etype in ("mousedown", "mouseup", "mousemove"):
            cdp.send("Input.dispatchMouseEvent", {
                "type": {"mousedown": "mousePressed", "mouseup": "mouseReleased",
                         "mousemove": "mouseMoved"}[etype],
                "x": x, "y": y,
                "button": ev.get("button", "left"),
                "buttons": 1 if etype != "mousemove" else 0,
                "clickCount": int(ev.get("clickCount", 1)),
            })
        elif etype == "wheel":
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y,
                "deltaX": float(ev.get("deltaX", 0)),
                "deltaY": float(ev.get("deltaY", 0)),
            })
        elif etype == "text":
            cdp.send("Input.insertText", {"text": str(ev.get("text", ""))[:500]})
        elif etype == "key":
            # non-printable keys: Enter, Backspace, Tab, arrows, Escape, Delete
            key = str(ev.get("key", ""))[:20]
            down = {"type": "rawKeyDown", "key": key}
            vk = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27,
                  "Delete": 46, "ArrowLeft": 37, "ArrowUp": 38,
                  "ArrowRight": 39, "ArrowDown": 40, "Home": 36, "End": 35}.get(key)
            if vk:
                down["windowsVirtualKeyCode"] = vk
                down["nativeVirtualKeyCode"] = vk
            if key == "Enter":
                down["text"] = "\r"
                down["type"] = "keyDown"
            cdp.send("Input.dispatchKeyEvent", down)
            cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key,
                                                **({"windowsVirtualKeyCode": vk,
                                                    "nativeVirtualKeyCode": vk} if vk else {})})

    # callbacks -- run inside the session thread's event pump
    def _on_frame(self, cdp, params):
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        try:
            from base64 import b64decode
            data = b64decode(params["data"])
            with self.lock:
                self.frame = data
                self.frame_seq += 1
        except Exception:
            pass

    def _on_step(self, source, payload):
        try:
            step = json.loads(payload)
        except (TypeError, ValueError):
            return
        if not isinstance(step, dict) or "action" not in step:
            return
        step = {"action": str(step.get("action"))[:20],
                "selector": str(step.get("selector", ""))[:300],
                "value": str(step.get("value", ""))[:1000]}
        with self.lock:
            # coalesce: repeated typing in one field is ONE fill
            if (step["action"] == "fill" and self.steps
                    and self.steps[-1]["action"] == "fill"
                    and self.steps[-1]["selector"] == step["selector"]):
                self.steps[-1] = step
            else:
                self.steps.append(step)


# ---------------------------------------------------------------------------
# steps -> python code
# ---------------------------------------------------------------------------

def _pyquote(text: str) -> str:
    # Newlines/tabs are REAL in recorded values (a textarea fill, a
    # "check it says..." on multi-line text) -- left raw they split the
    # string literal and the generated test file cannot even parse.
    s = (str(text).replace("\\", "\\\\").replace('"', '\\"')
         .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return '"' + s + '"'


def needs_expect(steps) -> bool:
    return any(str(s.get("action", "")).startswith("assert") or
               s.get("action") == "wait_text" for s in steps)


def build_step_lines(steps) -> list:
    """The recorded actions as unindented python lines."""
    lines = []
    for step in steps:
        sel = _pyquote(step.get("selector", "body"))
        val = step.get("value", "")
        action = step.get("action")
        timeout = step.get("timeout_ms")
        t_arg = f", timeout={int(timeout)}" if timeout else ""
        if action == "click":
            lines.append(f"page.click({sel})")
        elif action == "fill":
            lines.append(f"page.fill({sel}, {_pyquote(val)})")
        elif action == "select":
            lines.append(f"page.select_option({sel}, {_pyquote(val)})")
        elif action == "press":
            lines.append(f"page.press({sel}, {_pyquote(val)})")
        # ---- checks & waits from the right-click palette --------------------
        elif action == "assert_text":
            lines.append(f"expect(page.locator({sel})).to_contain_text({_pyquote(val)}{t_arg})")
        elif action == "assert_value":
            lines.append(f"expect(page.locator({sel})).to_have_value({_pyquote(val)}{t_arg})")
        elif action == "assert_visible":
            arg = f"timeout={int(timeout)}" if timeout else ""
            lines.append(f"expect(page.locator({sel})).to_be_visible({arg})")
        elif action == "assert_css":
            prop = _pyquote(step.get("extra", "color"))
            lines.append(f"expect(page.locator({sel})).to_have_css({prop}, {_pyquote(val)}{t_arg})")
        elif action == "wait_visible":
            lines.append(f"page.wait_for_selector({sel}, state=\"visible\"{t_arg})")
        elif action == "wait_hidden":
            lines.append(f"page.wait_for_selector({sel}, state=\"hidden\"{t_arg})")
        elif action == "wait_text":
            lines.append(f"expect(page.locator({sel})).to_contain_text({_pyquote(val)}{t_arg})")
    return lines


def build_code(steps, target_url, start_path="") -> str:
    lines = ["def run(page, ctx):"]
    if needs_expect(steps):
        lines.append("    from playwright.sync_api import expect")
    lines.append("    " + goto_line_py(start_path))
    step_lines = build_step_lines(steps)
    lines += ["    " + l for l in step_lines]
    if not step_lines:
        lines.append("    # (no interactions were recorded)")
    lines += ["",
              "    ctx.screenshot(\"end\")",
              "    # TODO: assert something real about the result, e.g.:",
              "    # assert \"expected text\" in page.inner_text(\"#result\")"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# TypeScript output -- the SAME semantic steps, emitted for @playwright/test.
# The recording never knows a language; only these emitters do, which is why
# one session can offer both.
# ---------------------------------------------------------------------------

def _tsquote(text: str) -> str:
    # same reason as _pyquote: raw newlines/tabs in a recorded value must
    # not split the '...' literal in the generated spec
    s = (str(text).replace("\\", "\\\\").replace("'", "\\'")
         .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return "'" + s + "'"


def build_step_lines_ts(steps) -> list:
    """The recorded actions as unindented TypeScript lines."""
    lines = []
    for step in steps:
        sel = _tsquote(step.get("selector", "body"))
        val = step.get("value", "")
        action = step.get("action")
        timeout = step.get("timeout_ms")
        t_opt = f", {{ timeout: {int(timeout)} }}" if timeout else ""
        t_only = f"{{ timeout: {int(timeout)} }}" if timeout else ""
        if action == "click":
            lines.append(f"await page.click({sel});")
        elif action == "fill":
            lines.append(f"await page.fill({sel}, {_tsquote(val)});")
        elif action == "select":
            lines.append(f"await page.selectOption({sel}, {_tsquote(val)});")
        elif action == "press":
            lines.append(f"await page.press({sel}, {_tsquote(val)});")
        elif action == "assert_text":
            lines.append(f"await expect(page.locator({sel})).toContainText({_tsquote(val)}{t_opt});")
        elif action == "assert_value":
            lines.append(f"await expect(page.locator({sel})).toHaveValue({_tsquote(val)}{t_opt});")
        elif action == "assert_visible":
            lines.append(f"await expect(page.locator({sel})).toBeVisible({t_only});")
        elif action == "assert_css":
            prop = _tsquote(step.get("extra", "color"))
            lines.append(f"await expect(page.locator({sel})).toHaveCSS({prop}, {_tsquote(val)}{t_opt});")
        elif action == "wait_visible":
            lines.append(f"await page.waitForSelector({sel}, {{ state: 'visible'{', timeout: %d' % int(timeout) if timeout else ''} }});")
        elif action == "wait_hidden":
            lines.append(f"await page.waitForSelector({sel}, {{ state: 'hidden'{', timeout: %d' % int(timeout) if timeout else ''} }});")
        elif action == "wait_text":
            lines.append(f"await expect(page.locator({sel})).toContainText({_tsquote(val)}{t_opt});")
    return lines


def build_code_ts(steps, target_url, start_path="") -> str:
    step_lines = build_step_lines_ts(steps)
    lines = ["import { test, expect } from '@playwright/test';",
             "",
             "test('recorded flow', async ({ page }) => {",
             "  " + goto_line_ts(start_path) + "   // relative to the hub's target.url"]
    lines += ["  " + l for l in step_lines]
    if not step_lines:
        lines.append("  // (no interactions were recorded)")
    lines += ["",
              "  // TODO: assert something real about the result, e.g.:",
              "  // await expect(page.locator('#result')).toContainText('expected text');",
              "});"]
    return "\n".join(lines) + "\n"


def append_steps_to_code_ts(code: str, steps, start_path="") -> str:
    """Recorded steps become a NEW test() block at the end of the spec file
    -- the natural unit of an @playwright/test file, and immune to the
    indentation surgery the python appender needs."""
    step_lines = build_step_lines_ts(steps)
    if not step_lines:
        return code
    out = code.rstrip("\n")
    if "@playwright/test" not in code:
        out = "import { test, expect } from '@playwright/test';\n\n" + out
    block = ["", "",
             "test('recorded addition', async ({ page }) => {",
             "  " + goto_line_ts(start_path)]
    block += ["  " + l for l in step_lines]
    block += ["});"]
    return out + "\n".join(block) + "\n"


def append_steps_to_code(code: str, steps, start_path="") -> "str | None":
    """Insert the recorded steps at the END of run()'s body (before any
    module-level code that follows). None if the file has no run(). A
    recording that began on another page first navigates there."""
    step_lines = build_step_lines(steps)
    if not step_lines:
        return code
    if start_path:
        step_lines = [goto_line_py(start_path)] + step_lines
    lines = code.rstrip("\n").split("\n")
    def_idx = next((i for i, l in enumerate(lines) if l.startswith("def run(")), None)
    if def_idx is None:
        return None
    # run()'s body ends before the first non-empty column-0 line after it
    end = len(lines)
    for i in range(def_idx + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t")):
            end = i
            break
    header = ["", "    # --- appended from a recording ---"]
    if needs_expect(steps) and "playwright.sync_api import expect" not in code:
        header.append("    from playwright.sync_api import expect")
    block = header + ["    " + l for l in step_lines]
    new_lines = lines[:end] + block + lines[end:]
    return "\n".join(new_lines) + "\n"


# ---------------------------------------------------------------------------
# module-level session management (one at a time, like the codegen recorder)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_session = None


def start(url: str = "", test_id: str = "", start_path: str = "") -> dict:
    """Begin a session at the target (or at start_path under it). Raises
    ValueError for a start path that would leave the site under test."""
    global _session
    cfg = appconfig.get_config()
    start_path = clean_start_path(start_path)
    prior_steps = []
    if test_id:
        # continuation recording: show what the test already does
        from core.models import Test
        from core.services.syncer import read_test_code
        try:
            test = Test.objects.get(test_id=test_id, archived=False)
            prior_steps = extract_prior_steps(read_test_code(test))
        except Test.DoesNotExist:
            test_id = ""
    with _lock:
        if _session and _session.state in ("starting", "recording"):
            return {"ok": False, "error": "a remote recording session is already running"}
        _session = RecorderSession(url or cfg.target_url, test_id=test_id,
                                   prior_steps=prior_steps, start_path=start_path)
    return {"ok": True}


def take_pending() -> dict:
    """Consume a finished recording: {code, steps, test_id} or {}. Whoever
    lands the handoff (the new-test form, an append/replace apply) calls
    this exactly once; afterwards the session is gone."""
    global _session
    with _lock:
        if _session is None or _session.state != "done":
            return {}
        out = {"code": _session.code, "code_ts": _session.code_ts,
               "steps": list(_session.steps), "test_id": _session.test_id,
               "start_path": _session.start_path}
        _session = None
        return out


def current():
    with _lock:
        return _session


def status() -> dict:
    # Bind the global ONCE under the lock. The recorder page polls this ~2x a
    # second from every open tab while clear()/start() may be reassigning
    # _session, so re-reading it mid-function can hit None and raise.
    session = current()
    if session is None:
        return {"state": "idle"}
    snap = session.snapshot()
    if snap["state"] == "done":
        snap["code"] = session.code
        snap["code_ts"] = session.code_ts
    return snap


def stop() -> dict:
    session = current()
    if session is None:
        return {"state": "idle"}
    session.stop()
    # give the session thread a moment to close and build the code
    for _ in range(60):
        if session.state in ("done", "error"):
            break
        time.sleep(0.1)
    return status()


def clear():
    global _session
    with _lock:
        if _session and _session.state in ("starting", "recording"):
            _session.stop()
        _session = None

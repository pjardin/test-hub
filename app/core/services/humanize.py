"""Turn test code into plain language, so a non-technical tester can read
what a test does without reading Python. Best-effort line-by-line: known
patterns become sentences, anything else stays as (dimmed) code."""
import re

_PATTERNS = [
    (re.compile(r'page\.goto\(ctx\.base_url\)'),
     lambda m: "Open the app's page"),
    (re.compile(r'page\.goto\("(?P<u>[^"]+)"\)'),
     lambda m: f"Open {m.group('u')}"),
    (re.compile(r'page\.fill\("(?P<s>[^"]+)",\s*"(?P<v>[^"]*)"\)'),
     lambda m: f"Type “{m.group('v')}” into {m.group('s')}"),
    (re.compile(r'page\.click\("(?P<s>[^"]+)"\)'),
     lambda m: f"Click {m.group('s')}"),
    (re.compile(r'page\.dblclick\("(?P<s>[^"]+)"\)'),
     lambda m: f"Double-click {m.group('s')}"),
    (re.compile(r'page\.select_option\("(?P<s>[^"]+)",\s*"(?P<v>[^"]*)"\)'),
     lambda m: f"Choose “{m.group('v')}” in {m.group('s')}"),
    (re.compile(r'page\.press\("(?P<s>[^"]+)",\s*"(?P<v>[^"]*)"\)'),
     lambda m: f"Press {m.group('v')} in {m.group('s')}"),
    (re.compile(r'page\.check\("(?P<s>[^"]+)"\)'),
     lambda m: f"Tick the checkbox {m.group('s')}"),
    (re.compile(r'page\.wait_for_selector\("(?P<s>[^"]+)",\s*state="visible"(?:,\s*timeout=(?P<t>\d+))?\)'),
     lambda m: f"Wait until {m.group('s')} appears" + _upto(m.group('t'))),
    (re.compile(r'page\.wait_for_selector\("(?P<s>[^"]+)",\s*state="hidden"(?:,\s*timeout=(?P<t>\d+))?\)'),
     lambda m: f"Wait until {m.group('s')} is gone" + _upto_placeholder(m)),
    (re.compile(r'page\.wait_for_selector\("(?P<s>[^"]+)"(?:,\s*timeout=(?P<t>\d+))?\)'),
     lambda m: f"Wait until {m.group('s')} appears" + _upto_placeholder(m)),
    (re.compile(r'page\.wait_for_timeout\((?P<t>\d+)\)'),
     lambda m: f"Wait {_secs(m.group('t'))} (fixed pause)"),
    (re.compile(r'page\.wait_for_url\("(?P<u>[^"]+)"\)'),
     lambda m: f"Wait for the address to become {m.group('u')}"),
    (re.compile(r'expect\(page\.locator\("(?P<s>[^"]+)"\)\)\.to_contain_text\("(?P<v>[^"]*)"(?:,\s*timeout=(?P<t>\d+))?\)'),
     lambda m: f"Check {m.group('s')} says “{m.group('v')}”" + _upto_placeholder(m)),
    (re.compile(r'expect\(page\.locator\("(?P<s>[^"]+)"\)\)\.to_have_value\("(?P<v>[^"]*)"(?:,\s*timeout=\d+)?\)'),
     lambda m: f"Check the value of {m.group('s')} is “{m.group('v')}”"),
    (re.compile(r'expect\(page\.locator\("(?P<s>[^"]+)"\)\)\.to_be_visible\((?:timeout=\d+)?\)'),
     lambda m: f"Check {m.group('s')} is visible"),
    (re.compile(r'expect\(page\.locator\("(?P<s>[^"]+)"\)\)\.to_have_css\("color",\s*"(?P<v>[^"]*)"[^)]*\)'),
     lambda m: f"Check the text color of {m.group('s')} is {m.group('v')}"),
    (re.compile(r'expect\(page\.locator\("(?P<s>[^"]+)"\)\)\.to_have_css\("background-color",\s*"(?P<v>[^"]*)"[^)]*\)'),
     lambda m: f"Check the background color of {m.group('s')} is {m.group('v')}"),
    (re.compile(r'with ctx\.timed\("(?P<v>[^"]*)"\):'),
     lambda m: f"⏱ Timed phase: {m.group('v')}"),
    (re.compile(r'ctx\.screenshot\("(?P<v>[^"]*)"\)'),
     lambda m: f"Take a screenshot (“{m.group('v')}”)"),
    (re.compile(r'ctx\.log\("(?P<v>[^"]*)"\)'),
     lambda m: f"Note in the log: “{m.group('v')}”"),
    (re.compile(r'assert\s+"(?P<v>[^"]+)"\s+in\s+page\.inner_text\("(?P<s>[^"]+)"\)'),
     lambda m: f"Check {m.group('s')} contains “{m.group('v')}”"),

    # --- locator style -----------------------------------------------------
    # page.locator("#x").click() and friends. This is what the Playwright
    # docs teach and what most copied-in code looks like, so without these a
    # whole test reads as raw Python to the people this page exists for.
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.fill\("(?P<v>[^"]*)"\)'),
     lambda m: f"Type “{m.group('v')}” into {m.group('s')}"),
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.click\(\)'),
     lambda m: f"Click {m.group('s')}"),
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.dblclick\(\)'),
     lambda m: f"Double-click {m.group('s')}"),
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.check\(\)'),
     lambda m: f"Tick the checkbox {m.group('s')}"),
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.select_option\("(?P<v>[^"]*)"\)'),
     lambda m: f"Choose “{m.group('v')}” in {m.group('s')}"),
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.press\("(?P<v>[^"]*)"\)'),
     lambda m: f"Press {m.group('v')} in {m.group('s')}"),
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.wait_for\(\s*state="hidden"(?:,\s*timeout=(?P<t>\d+))?\s*\)'),
     lambda m: f"Wait until {m.group('s')} is gone" + _upto_placeholder(m)),
    (re.compile(r'page\.locator\("(?P<s>[^"]+)"\)\.wait_for\((?:\s*state="visible")?(?:,?\s*timeout=(?P<t>\d+))?\s*\)'),
     lambda m: f"Wait until {m.group('s')} appears" + _upto_placeholder(m)),

    # --- get_by_* (the recommended locators) --------------------------------
    (re.compile(r'page\.get_by_role\("(?P<r>[^"]+)",\s*name="(?P<n>[^"]*)"\)\.click\(\)'),
     lambda m: f"Click the {m.group('r')} labelled “{m.group('n')}”"),
    (re.compile(r'page\.get_by_label\("(?P<n>[^"]+)"\)\.fill\("(?P<v>[^"]*)"\)'),
     lambda m: f"Type “{m.group('v')}” into the “{m.group('n')}” field"),
    (re.compile(r'page\.get_by_text\("(?P<n>[^"]+)"\)\.click\(\)'),
     lambda m: f"Click the text “{m.group('n')}”"),
]


def _secs(ms):
    try:
        s = int(ms) / 1000
    except (TypeError, ValueError):
        return "?"
    if s >= 3600:
        return f"{s/3600:g} h"
    if s >= 60:
        return f"{s/60:g} min"
    return f"{s:g} s"


def _upto(t):
    return f" (up to {_secs(t)})" if t else ""


def _upto_placeholder(m):
    return _upto(m.groupdict().get("t"))


def humanize_code(code: str) -> list:
    """[{kind: 'step'|'code'|'phase', text}] for the run() body, in order."""
    out = []
    in_run = False
    for raw in (code or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("def run("):
            in_run = True
            continue
        if not in_run or not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("\"\"\"", "'''", "from ", "import ")):
            continue
        matched = False
        for pattern, render in _PATTERNS:
            m = pattern.match(stripped)
            if m:
                text = render(m)
                out.append({"kind": "phase" if text.startswith("⏱") else "step",
                            "text": text})
                matched = True
                break
        if not matched:
            out.append({"kind": "code", "text": stripped[:120]})
    return out

# Test Hub — the manual

A self-hosted web app for running, scheduling and analysing Playwright tests
against **one target website**. It runs identically on your laptop, on an
office machine, or on AWS — the only thing that changes between machines is
one `config.json`.

Built for the same world as the rest of this repo: **air-gapped RHEL 8.10 /
CentOS 7.9 boxes**, updated by carrying zips on a disk.

> **Reading this in the laptop edition (`test-hub`)?** This manual is shared
> with the offline-deployment project and written for installed lab
> machines. In this repository:
> - `testhub <command>` is `./testhub.sh manage <command>`, with shortcuts
>   `./testhub.sh start | stop | status | run | demo-release`;
>   `./app/dev.sh` is `./testhub.sh setup` then `start`, and
>   `./app/dev.sh test` is `./testhub.sh unit-tests`;
> - `/opt/pw-testhub/` is this folder, the TypeScript engine is `.pw-ts/`
>   rather than `/opt/pw-ts`, and the hub listens on http://127.0.0.1:8880/;
> - `make …` targets, `docs/AWS.md`, `appbundle/`, `appkit_ts_py/`,
>   `scripts/`, the installers and kits, pwlab, fapolicyd and systemd all
>   belong to the main project and do not apply here.

---

## The mental model (read this first)

**Two URLs, two different things:**

| | config key | example |
|---|---|---|
| where the **hub** lives | `site.host` / `site.port` | `0.0.0.0:8880` on AWS, `127.0.0.1:8880` on your laptop |
| what the hub **tests** | `target.url` | `https://your-app.internal/` — or empty for the built-in `/demo/` page |

**Files are the source of truth for test definitions; the database owns run
history.** The `data/tests/` folder holds one `.py` + one `.json` per test
plus `_groups.json`. Booting the hub (or pressing *Rescan*) re-populates the
DB from those files; editing anything in the UI immediately rewrites the
files. So the tests folder can always be copied to another machine — via
git, scp or a burned disk — and nothing is lost. Run history (the `results/`
folder and `db.sqlite3`) stays on the machine that executed the runs.

**One process.** `manage.py serve` runs the web UI (waitress), the run queue
with its worker pool, and the weekly scheduler in a single process. No
message broker, no second service — nothing extra to install on a
government box. `runserver` (Django's dev server) gives you a read-only UI
and says so in a banner.

---

## Running it

### On your laptop (internet, for development and authoring)

```bash
./app/dev.sh          # venv + Django + Playwright + chromium, seeds samples,
                      # then serves http://127.0.0.1:8880/
./app/dev.sh test     # run the unit test suite
```

A standalone copy of the hub for laptops (the `test-hub` repository) wraps
the same thing in `./testhub.sh setup | start | stop | status`, with its
own README.

### Under pwlab (the docker everything-image, e.g. an EC2 box)

The hub runs as a container service there: `pwlab hub` serves on :8000
(usually already enabled by `ec2-setup.sh` as `pwlab-hub.service`).
Terminal commands work two ways — from the HOST prefix them
(`pwlab testhub runtest DEMO-001`, `pwlab testhub tui`), or open
`pwlab shell` / the RDP desktop and use plain `testhub …` inside.
Tests, history and secrets persist in the `pwlab-hub-data` volume;
everything else in this manual applies unchanged.

### On an offline target (RHEL 8 / CentOS 7)

**The easy way — the lab kit.** `make labkit` assembles `dist/labkit/`
(both base zips + app zip + installer + a printable `LAB-INSTALL.md`);
copy the folder to a disk and on the machine run
`sudo ./install-all.sh --service` — it detects the OS and installs both
layers. `make test-labkit` proves that exact kit offline on both OSes
before you burn it.

**By hand — two zips, installed in order** — see `appbundle/INSTALL-APP.md`
(a copy travels inside the app zip):

```bash
sudo ./install.sh                    # base bundle: Python+Playwright+browsers
sudo ./install-app.sh [--service]    # app bundle: the Test Hub
sudo vi /opt/pw-testhub/config.json  # target.url, site.host
/opt/pw-testhub/bin/testhub serve    # or: systemctl start pw-testhub
```

`bin/testhub` is a thin launcher that sources the base bundle's `env.sh`
(that is what makes the offline browsers findable) and then runs
`manage.py` with the bundle's Python. Use it for everything:
`testhub serve`, `testhub sync`, `testhub runtest DEMO-001`.

### On AWS / the office network

Simplest form: set `site.host` to `0.0.0.0`, open the port in the security
group / firewall, browse to `http://<machine>:8880/`.

To serve the hub **under your existing app's URL** —
`https://app.example.com/testhub/` behind the same ALB and certificate —
set `site.url_prefix`, `site.public_url` and `site.behind_proxy` and add an
ALB path rule. **`docs/AWS.md`** (in the main project) is the step-by-step for the
deployed shape — one EC2 instance reached by IP; putting it behind a real
domain/ALB (with OIDC/Cognito login) is noted there as a later add-on.

**There is no login built in** — treat the hub like a CI admin page:
restrict by network, or put ALB authentication in front (docs/AWS.md).
Never expose it to the public internet.

---

## config.json reference

Search order: `$PW_TESTHUB_CONFIG`, then `<home>/config.json` where
`<home>` is the parent of the `app/` folder (`/opt/pw-testhub` on targets,
the repo root in dev). Missing keys fall back to defaults, so a minimal
file is fine.

```jsonc
{
  "site":   { "host": "127.0.0.1",      // 0.0.0.0 to serve the network
              "port": 8880,
              "public_url": "" },        // display-only override
  "target": { "name": "My application",
              "url": "",                 // "" = built-in /demo/ page
              "version": "2.3.1" },      // stamped on every run -> charts
  "paths":  { "data_dir": "" },          // "" = <home>/data
  "runner": {
      "max_parallel": 2,                 // how many tests run at once
      "default_timeout_seconds": 300,    // per test; sidecar can override
      "browser": "chromium",             // chromium | chrome | msedge | system-chromium
      "headed": false,                   // true needs a display / xvfb
      "video": true, "trace": true,
      "live_screenshots": true,          // the live "watch it run" view
      "activity_frames": true,           // the activity reel (see "Activity replay")
      "python": "" },                    // harness interpreter override
  "schedule": {
      "timezone": "America/Los_Angeles", // weekly schedules fire in this tz
      "catchup_minutes": 60 },           // fire late if hub was down < this
  "debug": false
}
```

`target.version` is the one to keep current: bump it when the application
under test is upgraded, and every subsequent run is recorded against the
new version — that is what feeds the *outcomes by version* charts. It is
editable on the **Settings** page (which writes config.json for you).
While `target.url` is empty (the built-in demo), the demo reports its own
version instead — see the next section.

---

## The built-in demo site: Acme Freight

The hub serves a complete make-believe business app to test, entirely
offline: **Acme Freight**, at `/demo/freight/` (the header's *built-in demo
site* link). The classic one-page demo at `/demo/` is still there, unchanged,
for DEMO-001…009.

| part | what it exercises |
|---|---|
| public site: tracking with a world map, a 4-step quote wizard with promo codes, bookings | forms, validation messages, multi-page flows, money checks |
| staff portal (sign-in `tester` or `manager`, password `demo-password`): dashboard, filterable shipments table, notes, confirm dialogs, manager-only reports | auth, role checks, tables, JSON requests, browser dialogs |
| **route optimizer** (20–50 s), **manifest import** (upload a CSV, download the result), **monthly report** (about 2 min) — real server-side jobs with live progress | slow jobs, one-wait-for-a-result, file upload/download, long waits |
| **public REST API** (`/demo/freight/api/v1/`; keys and a *Try it* button on the **Developers** page) — shipments, quotes, a per-key rate limit | API tests with no page (`page.request`), JSON contracts, 401/429, soft assertions |
| **dock schedule** — drag arriving trucks onto dock doors; the server enforces the yard's rules (hazmat door, forklifts for heavy loads) | drag-and-drop, server-side validation, state that survives a reload |
| **live fleet map** — trucks drive their routes in real time, arrivals stream into a feed | waiting for one specific event on a page that never stops moving |
| **password reset by email** — *Forgot your password?* sends a single-use link; every email lands in the demo's **Mailbox** page (a test mail catcher: nothing leaves the network), a few seconds after it is sent | flows that go through email: waiting for the message, correlating by reference, following the link |
| **shipping labels** — *Print label* on a shipment opens a 4×6 label with a real Code 128 barcode in a new tab, plus a PDF | new tabs (`expect_popup`), reading a barcode from its bars, PDF downloads |
| **status page** (`/demo/freight/status/`, `status.json`) — components, 30 days of uptime, past incidents; stays up during an outage | what an incident looks like from outside |
| accessibility — every page is WCAG 2.1 AA clean on 1.x and 3.0 (checked with axe-core, shipped with the samples) | accessibility scanning, Section 508 |

Everything runs on the hub's server: maps and cities are Natural Earth data
(public domain) bundled in the app zip, shipments are generated from a fixed
seed (the same AF- numbers on every machine), prices are exact decimals.

**Five releases.** The demo's **Release console** (`/demo/freight/releases/`,
or `testhub demo_release 2.0.0` from a terminal) "deploys" one; each changes
something a tester should notice, and each change is caught by a different
hub feature:

| release | what changes | where the hub shows it |
|---|---|---|
| 1.0.0 Launch | baseline | — |
| 1.1.0 Faster search | tracking, search and imports get faster | Analytics speed table, step trends |
| 2.0.0 The redesign | new look everywhere; optimizer ~2x slower; every 3rd note save fails; the API renames `eta` to `estimated_delivery` without a word in its changelog; accessibility breaks (no page language, skip link hidden from screen readers, the map loses its text alternative, brand colours at 2.86:1 contrast) | DEMO-017 visual diff; DEMO-014 timing; DEMO-013 pass rate; DEMO-020 and DEMO-022 fail |
| 2.1.0 Hotfix | no session rotation, CSP + X-Frame-Options dropped, a call to an outside host, promo applied twice (in the API too); the trial key's rate limit is gone; "faster reset emails" leave reset links working after use; 2.0's accessibility problems stay | DEMO-012, DEMO-011, DEMO-020, DEMO-022 and DEMO-023 fail; Security page (2 high) |
| 3.0.0 Stable | all fixed, fastest yet; the API sends both field names and marks `eta` deprecated; accessible brand colours | everything green; "faster" across Analytics |

While the hub tests the demo (`target.url` empty), **every run is stamped
with the deployed release** — also runs queued from a terminal — so the
Analytics page compares releases with no configuration at all (config's
`target.version` applies to a real target only). The console's
*What the Test Hub should notice* spoilers, and the demo's **Guided tour**
page (`/demo/freight/tour/`), walk through it: run the **Freight regression**
group, deploy the next release, run it again, open Analytics.

**Incidents.** The console's *Incident simulator* breaks the site on
purpose, independent of the release: *Take the site down* makes every Acme
page answer 503 with a maintenance page (the API answers 503 JSON; the
console itself stays up so you can end it), *Make it slow* adds 2.5 s to
every page, *All clear* ends it (terminal: `testhub demo_release --incident
outage|slow|clear`). Run **Freight smoke** during an outage: each test fails
within seconds saying the site answered HTTP 503 — the page objects check
the status of every page they open — instead of timing out on a missing
button. The public **status page** (`/demo/freight/status/`) stays up and
shows the incident from outside, as a real one would. Runs made during an
incident are stamped with the deployed release like any other, so end the
incident before comparing releases on Analytics.

The sample tests **DEMO-010…024** drive it, with page objects in
`_lib/freight.py`; groups **Freight smoke**, **Freight regression**,
**Freight platform** (the API, drag-and-drop and live-map tests) and
**Freight extras** (the accessibility scan, the password reset through
email, the shipping label) ship with them.

Six of those checks also exist in **TypeScript** — **DEMO-T11…T16**: the
quote, tracking, the public API (`request` fixture + `expect.soft`), dock
drag-and-drop (`locator.dragTo`), the shipping label (a popup tab, the
barcode decoded by the test's own Code 128 reader, the PDF download) and
the password reset through the mailbox (on its own account, `courier`, so
it can run beside the Python one). Shared helpers live in
`_lib/freight.ts`. Group **Freight in TypeScript** runs the six; group
**Python and TypeScript, side by side** runs all twelve, so any pair's run
pages can be compared — and each pair reaches the same verdict on every
release (2.0.0 breaks the API in both, 2.1.0 the promo code, the API and
the reset link in both).

**Accessibility checks for your own site** come with the samples:
`_lib/a11y.py` runs axe-core (the engine most commercial scanners use,
vendored in `_lib/axe/` with its MPL-2.0 licence, so it works offline)
against WCAG 2.1 A and AA — the standard Section 508 points to:

```python
from _lib.a11y import Audit
audit = Audit(page, ctx)
audit.scan("home page")      # after each page you want checked
audit.save_report()          # a11y-report.html + .json, listed on the run page
audit.assert_clean()         # fails on critical and serious findings
```

A scan finds what a machine can find — about a third of WCAG — and does
not replace a review with a screen reader; it just never forgets to look.
 **After an upgrade** they are not there yet — an
upgrade never touches your tests folder — so run `testhub samples` to list
what the new version ships and `testhub samples --add-new` to copy it
(nothing existing is overwritten, archived tests stay archived, your groups
are kept). A sample's code that you already have but that differs from
the new version's copy — a test file, or a shared file such as
`_lib/freight.py` — is listed with a star (`test*`, `lib*`) and left alone:
it may be an older sample or it may hold your own edits.
`testhub samples --update` takes the new copy and keeps yours beside it as
`<name>.before-<version>` (merge back any edits of your own). Sidecars
(schedules, settings) and groups are never replaced.

---

## Writing tests

### The file pair

```
data/tests/LOGIN-001__login_smoke.py      the code
data/tests/LOGIN-001__login_smoke.json    the metadata sidecar
```

The **test id** is everything before the first `__` in the filename
(`LOGIN-001`). It is the permanent key: history, graphs and comparisons
follow the id across renames, machines and re-syncs. Ids must be unique;
the sync reports duplicates and ignores the second file.

Since v2.11.0 the hub is **bilingual**: a test can also be a TypeScript
`@playwright/test` spec — `PAY-001__refund_flow.spec.ts` with the same
`.json` sidecar — running on the TS engine's node instead of the bundled
Python. Everything in this manual that is not about Python code applies
to both. See “TypeScript tests” below for what that requires and how it
behaves.

### The code contract

```python
def run(page, ctx):
    page.goto(ctx.base_url)          # the target URL from config.json
    page.fill("#user", "worker1")
    ctx.log("logged in")             # goes to the run log, timestamped
    ctx.screenshot("after-login")    # saved into the run's artifacts
    assert page.title() == "Home"    # AssertionError (or any exception) = FAILED
```

`page` is a plain Playwright sync-API `Page` — anything Playwright can do
works here — and **every action on it is timed automatically**: each
`goto`/`click`/`fill`/`wait_for_*`/… call becomes one entry in the run's
*Step timings* (e.g. `click #submit-btn — 37ms`). Wrap larger phases in
`with ctx.timed("login flow"):` to time them as one named block. The harness owns browser launch/teardown, video, trace, the
failure screenshot, timeouts and kill handling; the test only does test
things. Only conservative Playwright APIs are used by the harness itself,
so the same tests run on Playwright 1.60 (RHEL 8), 1.35 (CentOS 7) and
current versions (your laptop).

### The sidecar

```json
{
  "id": "LOGIN-001",
  "name": "Login smoke test",
  "description": "Basic login with a valid user.",
  "tags": ["smoke", "login"],
  "version": "2.3",
  "timeout_seconds": 120,
  "enabled": true,
  "schedules": [
    { "days": ["mon", "wed", "fri"], "time": "09:00", "enabled": true }
  ]
}
```

- `tags` — free-form, used for filtering/categorising in the UI.
- `schedules[].browser` — optional: which browser those scheduled runs
  simulate on (`chromium`, `chrome`, `msedge`, `system-chromium`); omitted =
  the config default. The same field works in `_groups.json` schedules.
- `version` — which target version this test is associated with (e.g. the
  version that introduced the feature). Free-form.
- `timeout_seconds: 0` means "use the runner default".
- `schedules[].days` — `mon..sun` names (or 0–6, Monday=0).
- Missing sidecar? The sync creates a skeleton one automatically.

### Groups — `data/tests/_groups.json`

```json
{
  "groups": [
    { "name": "Nightly regression",
      "description": "Everything that must pass before a release.",
      "tests": ["LOGIN-001", "CART-002", "REPORT-003"],
      "schedules": [ { "days": ["tue", "thu"], "time": "02:00", "enabled": true } ] }
  ]
}
```

You almost never edit these files by hand — the **New test** page, the
group pages and the schedule pages write them for you — but because they
*are* the storage, hand-editing + *Rescan* is always a valid path, and so
is dropping in files someone else wrote.

### Cookbook: checks, conditionals, waiting

**Pass/fail rule**: if `run(page, ctx)` returns, the test PASSED; any
uncaught exception fails it.

**Checks** — prefer `expect(...)` (auto-retries until true or timeout;
kills flakiness), plain `assert` for values already in hand:

```python
from playwright.sync_api import expect

expect(page.locator("#result")).to_contain_text("Saved")
expect(page.locator("#spinner")).to_be_hidden(timeout=30000)
expect(page).to_have_url("**/dashboard")
assert "Ada" in page.inner_text("#result"), "name missing from result"
```

**Conditionals** — for optional UI, check without waiting; for
two-outcome steps, wait for either then branch:

```python
banner = page.locator("#cookie-accept")
if banner.count() and banner.is_visible():   # answers NOW, no wait
    banner.click()

page.wait_for_selector("#success, .error-box", timeout=60000)
if page.locator(".error-box").is_visible():
    raise AssertionError(page.inner_text(".error-box"))
```

Never use a conditional to skip a failing check — only for UI that is
genuinely sometimes-there.

**Waiting** — never sleep; wait *for* something. Actions auto-wait for
their element; for everything else:

```python
page.wait_for_selector("#report", state="visible", timeout=120000)
page.wait_for_selector("#spinner", state="hidden")
page.wait_for_url("**/results")
page.wait_for_function("document.querySelectorAll('.row').length >= 10")

with ctx.timed("backend processing"):                       # long waits: big
    page.wait_for_selector("#done", timeout=2*60*60*1000)   # timeout + timed phase
```

`timeout_seconds` in the sidecar caps the whole test; every `page.*` wait
is auto-timed into Step timings; the activity reel skips the idle.
`wait_for_timeout(ms)` is a raw sleep — last resort only. (Everything
above works on both targets: Playwright 1.35 and 1.60.)

**Recorder outputs**: a recording is captured as language-neutral steps
(which element, what action, what value), so ONE recording can become
either language. The Remote Recorder shows a **Python / TypeScript
toggle** when you create a new test (the preview re-renders live);
recording *for an existing test* needs no choice — Append/Replace emit
that test's language automatically (a TS append lands as a fresh
`test()` block at the end of the spec). Native codegen (laptop) has the
stronger selector engine but emits Python only; the browser-tab recorder
works everywhere. Either way a recording captures *actions*, not
*meaning* — the final assertion is always yours to write.

### Comparing images / screenshots

The bundles ship **OpenCV**, **scikit-image**, **imagehash** and
**Pillow**, so "does this page still look right?" is a normal test.
`DEMO-005` is a complete working example: it screenshots the page,
compares against a baseline with SSIM, writes a diff image with the
changed areas marked in red, and fails when the difference exceeds a
threshold.

Baselines are **per machine** (a `.png` next to the test): fonts and
rendering differ between a laptop and a RHEL box, so a travelling
baseline would fail forever. The tests export/import carries only
`.py`/`.json`, so each machine blesses its own on first run. Delete the
baseline to re-bless after an intentional UI change.

### Four ways to create a test

1. **UI** — *Tests → New test*: metadata form + a real code editor
   (CodeMirror, fully offline): syntax highlighting, bracket
   matching, Ctrl-F search, Ctrl-/ comment, and autocomplete that knows
   the hub's API — type `page.` or `ctx.` (or Ctrl-Space) for the method
   list with signatures and one-line docs, plus ready-made snippets
   (assertions, timed blocks). A **Language** select picks Python or
   TypeScript (each starts from its own working template). Saving writes
   both files.
2. **Remote Recorder** (works everywhere, including a deployed hub) —
   *Record in a browser tab* (test pages) or *Record new test* on the Tests
   page (same-tab, no pop-up — Safari-friendly) shows a live picture of the
   **hub's own** bundled Chromium at the target URL. Click and type on the
   picture; input is injected into that browser, and a script inside the
   page captures each action *semantically* — which element, which
   selector, what value — so the output is `page.fill('#user', ...)`, never
   x/y coordinates. Press *Stop & generate code* and the
   `run(page, ctx)` code appears in the editor tab automatically.
   **Point-and-check (for non-technical testers)**: right-click any
   element in the streamed page and a plain-language palette opens —
   *Check it says…* (prefilled with the element's actual text), *Check its
   value*, *Check it's visible*, *Check text color / background*
   (prefilled with the live computed color, swatch included), and waits:
   *until it appears / until it's gone / until it says…* with a how-long
   picker (5 s – 1 hour). The element flashes an outline so you can see
   what you hit; every choice becomes a step and correct `expect(...)` /
   wait code. A tester can produce a complete, passing test — actions,
   waits and checks — without reading a line of Python.
   Because the recorder tab is a plain web page (an image stream plus
   fetch calls), it needs **no DevTools, no extensions, no display** —
   it works from a locked-down workstation Chrome against a headless
   AWS hub. Nothing ever runs on your workstation: the recorded browser
   is the server's. One session at a time; sessions idle out after 10
   minutes. Keep flows to clicks / typing / selects for the cleanest
   code, and always review the generated steps and finish the assertion.
   **Start on another page of the target** with `/recorder/?start=<path>`
   (e.g. `?start=freight/` opens the built-in Acme Freight demo — its
   footer links there): the path is relative to the target, cannot leave
   it, and the generated code replays from the same page
   (`page.goto(ctx.base_url + "freight/")`, TypeScript
   `await page.goto('freight/')`). TypeScript always navigates RELATIVE to
   `target.url` (`'./'`, never `'/'`, which would be the server root when
   the target lives under a path — as the demo does).
3. **Native codegen** (hub machine has a display) — *Record with codegen*
   opens Playwright's own recorder window locally; best-in-class selector
   inference when you are developing on the laptop.
4. **Files** — write the `.py`/`.json` pair (or a `.spec.ts` + `.json`
   pair) yourself, drop them in `data/tests/`, press *Rescan* (or
   `testhub sync`).

**With an AI agent.** The base bundle ships **OpenCode**; pointed at your
internal Claude-compatible endpoint it can write tests directly into
`data/tests/` in the house style — then press *Rescan* and run them. Setup
and prompt suggestions are in `docs/AI-AGENT.md` (a copy is installed at
`/opt/pw-offline/AI-AGENT.md`). Read and run whatever it produces before
trusting it: proving a test is real is the hub's job, not the agent's.

---

## TypeScript tests

Requires a TS engine at `/opt/pw-ts`. On RHEL 8 that is the **TS engine
add-on**: Playwright **1.58.2** exactly, the version the team's TypeScript
repos are written against. On CentOS 7 it is the **native CentOS 7
engine** (`tskit-centos7`): Playwright **1.35**, the newest that runs on
glibc 2.17. The 1.58 style (`getBy*`, web-first `expect`) runs unchanged
there, but the 2023+ APIs (`page.clock`, aria snapshots,
`addLocatorHandler`) do not exist, and `pw-tsc --noEmit` rejects a spec
that uses them before it ever runs. The docker everything-image runs the
exact 1.58.2 on either OS. On RHEL 8, the combination kit (`appkit_ts_py`,
guide `INSTALL-TS-PY.md`) installs the engine and the hub with one
installer.

A TypeScript test is a completely standard `@playwright/test` spec:

```ts
import { test, expect } from '@playwright/test';

test('refund flow', async ({ page }) => {
  await page.goto('./');                // the target itself (baseURL = target.url);
                                        // never '/': that is the server's root
  await test.step('fill and submit', async () => {   // shows in Step timings
    await page.fill('#amount', '20');
    await page.click('#submit');
  });
  await expect(page.locator('#status')).toContainText('refunded');
});
```

What is identical to Python tests: schedules, kill, batches, history,
analytics/CSV, load testing, backups, export/import (`.spec.ts` and
`_lib/*.ts` travel in test zips), the activity reel (extracted from the
trace's paint-triggered screenshots), video/trace toggles, and secrets —
the hub's store arrives as `PW_SECRET_<NAME>` environment variables,
redacted from logs, never written into spec files. **Step timings** too:
every page and API call and every web-first `expect` is timed
automatically (`click #login-btn`, `GET freight/api/v1/quotes`,
`toBeVisible #signed-in-user` — named by WHERE, never by the value typed or
sent), your `test.step()` blocks are phases around them like Python's
`ctx.timed()`, and on the run page the list follows the replay (see “Long
waits” below). The times come from the run's Playwright trace, which the
hub records whenever the reel is on; with reel and trace both off, only the
test and its `test.step()` blocks are listed. A failed TypeScript run's message lists
EVERY failed expectation — an `expect.soft()` goes on after a failure — each
with its expected and received values.

What is different, honestly: **passive security observation does not
apply** (it lives inside the Python harness; the UI says so on TS runs),
and browser choice maps to `chromium` / `chrome` / `msedge` only —
firefox/webkit are refused with a plain message because their TS-engine
builds cannot run on RHEL 8. The live console output keeps Playwright's
own list-reporter accent (`Running 2 tests … 2 passed`) rather than the
Python harness's log; the results the hub stores are one format either
way.

Manual runs: `testhub runtest PAY-001` works for both languages. A
`.spec.ts` can ALSO be run with plain `npx playwright test` against the
hub's tests dir, bypassing the hub entirely — the recipe (and the
env-var gotchas it needs) is in `appkit_ts_py/INSTALL-TS-PY.md`.

## Running tests

| what you asked for | how |
|---|---|
| run one test | *Run* button (tests list, test page, run page) |
| run several at once | tick checkboxes on *Tests* → *Run selected* |
| run a saved set together | *Groups* → *Run group* |
| run on a weekly cadence | *Schedules* → pick test-or-group, weekdays, time |

Every start creates a **batch** (even single runs), so progress, ETA and
kill have one shape everywhere. Parallelism is `runner.max_parallel`
workers pulling from one queue — a batch of 10 with 2 workers runs 2 at a
time until done.

**Schedules** fire in `schedule.timezone` (default Pacific —
`America/Los_Angeles`) while `serve` is running. Each schedule can pick
**which browser to simulate on** (chromium / chrome / msedge /
system-chromium — defaults to `runner.browser`), so the same suite can run
Mondays on Chrome and Wednesdays on Edge; every run records its browser
and the run tables can be filtered by it. If the hub was down at
slot time and comes back within `catchup_minutes`, the slot still fires;
later than that, it is skipped (logged, and visible as a stale *last
fired*). Each schedule fires at most once per slot.

**What ran, and how it went.** The *Groups* page lists **Recent group
runs** — every run of a group, however it was started (the *Run group*
button, a group's schedule, `testhub runtest --group`) — and the
*Schedules* page a **Schedule history** of every scheduled run, plus each
schedule's *Last run* with its result. Every entry opens that run's
**stats page** (the batch page): the result and pass rate, how long it took
(first start to last finish) and the test time added up, the website
release it ran against, and every test against what it **usually** takes —
the median of its earlier ordinary runs, never load-test iterations, shown
once there are at least three — flagged *slower* at +25% and *faster* at
−20%, with the first line of why anything failed. *Previous run* / *next
run* step through the same group's or schedule's runs. (Scheduled runs are
linked to their schedule since 2.16; older ones show their label, and so
does a run whose schedule was later deleted or moved to another time.)

**A group run in progress is one block on the dashboard** (*Running now*,
since 2.19): the group's name and how it was started, *3 of 8 done · 1
failed · 2 running · 3 queued*, and **how long until its last test should
be done** — with its running and waiting tests listed underneath, and a
*Kill group* button. That time comes from the hub simulating its own
queue: each running test holds a worker until its usual duration runs out,
each waiting test takes the next free worker, in queue order — so it counts
the work queued ahead of the group and knows that three 10-second tests on
two workers take 20 seconds, not 15. The bar shows time, not test count
(the tests left can be the long ones). A test with no history yet makes the
time *unknown* rather than a guess. The batch page's *est. total remaining*
is the same figure.

**Kill** — every running row has a *Kill* button (dashboard, run page,
batch page, runs list); batches have *Kill batch* (kills active runs,
cancels queued ones). Killed runs are excluded from duration statistics so
they cannot poison the ETA.

What Kill actually does, and why the order matters:

1. **SIGTERM the harness alone** — not the process group. The harness turns
   that into an exception inside your test, so it can stop cleanly.
2. **The verdict is written first.** `result.json` (status, per-step
   timings, your `ctx.log()` lines) lands on disk *before* the harness
   touches the browser again.
3. **Each post-mortem artifact gets its own time limit** — failure
   screenshot 10s, trace 30s, video 30s — and the first one to time out
   cancels the rest.
4. **SIGKILL to the whole group** as the backstop, for anything still
   standing.

Step 2 and 3 exist because of a real failure mode: killing a test
interrupts a Playwright *sync* call, which leaves its driver
mid-conversation, and the **next** call to that driver blocks forever. The
first version of this signalled the whole group at once and then took a
screenshot — the screenshot hung, the browser died underneath it, and the
run finished 67 seconds later with no `result.json`, no timings, no video
and a partial recording orphaned in `_video_tmp`. Now a killed run settles
in about 12 seconds and keeps its verdict and timings; when the browser has
stopped answering, the log says so in plain English:

```
[test] [  10.04s] KILLED
[harness] failure.png: gave up after 10s (the browser stopped responding)
[harness] video path: skipped (the browser is not responding)
[harness] browser teardown: skipped (the browser is not responding)
[harness] result: killed
```

A video is only guaranteed for runs that end on their own. That is a
Playwright constraint (the `.webm` is finalised by `context.close()`), not
a policy choice — which is exactly what the **activity reel** below is for:
its frames are written as the run goes, so they survive a kill.

**Stopping the server** (`Ctrl-C`, `systemctl stop`) is handled the same
way: in-flight runs are signalled, given time to finish writing, then
terminated, and marked `aborted` with a readable reason. Nothing is left
`running` forever and no Chromium is orphaned. (`serve` installs its own
SIGTERM handler for this — Python's default is to die instantly, without
running any cleanup.)

**ETA** — the estimate for a running test is the average of its own last
10 pass/fail durations. No history → the UI shows **null** ("not enough
data") rather than inventing a number. Batch ETA sums the members'
remaining estimates over the worker count, and is null if any member has
no history.

**Watching a run live** — open the run (dashboard → running row) in a
second window: a ~1 fps browser preview (CDP screencast; chromium-family
browsers), the log tail streaming, elapsed and remaining time. When the
run finishes the page swaps to the artifacts below.

**Activity replay** — the answer to "the video doesn't show what
happened". Browser-recorded webm only advances while the page paints, so a
test that works for two minutes and then waits an hour produces a video
where the wait collapses unpredictably (and the file lacks a duration
header, which makes players look truncated). The harness therefore also
records an **activity reel**: timestamped frames saved when the page
changes *and the test is actually doing something*. The run page plays it
back with a scrubber, speed control and wall-clock offsets, jumping over
idle stretches with a "skipped Xm of idle" marker — one continuous story
of what happened, however long the waits were. Config:
`runner.activity_frames` (on by default; chromium-family browsers).

Three rules keep the reel small, each catching what the one before it
cannot:

1. **Chromium sends nothing while nothing changes** — a genuinely static
   page costs zero frames.
2. **Identical frames are dropped** — a blinking caret loops between a few
   states forever, so "idle with a cursor in a field" stays idle.
3. **A page nobody is driving is sampled every ~15s** — this is the one
   that handles a **clock, countdown, progress bar or polling table**,
   where every repaint genuinely differs and rule 2 cannot help. The
   harness knows the difference because it sees every action the test
   issues: no action for 10 seconds means the test is waiting, not
   working.

Rule 3 matters more than it sounds. Measured on the demo page's job with a
per-second countdown, **an actual 30-minute run**: the reel holds **119
frames / 4.9 MB** — 6 frames across the busy first 15 seconds, then one
roughly every 16 seconds through the wait. Without idle sampling the same
run records one frame per countdown tick: ~1800 frames and ~74 MB of
near-identical pictures of a clock.

The last frame lands at 1801s of 1802s, so the moment the job finished is
captured. That is not automatic: the "done" state paints while the test is
still blocked inside `wait_for_selector`, so it looks like one more idle
repaint and the rate limiter drops it. The frame an idle page was last
seen in is therefore *held*, and written out the instant the test wakes
up. Without that, the reel ended 10.7 seconds *before* the completion —
missing the only frame anyone wanted.

Very long *busy* runs are additionally bounded by decimation (at ~1500
frames, every other frame is dropped and the capture interval doubles).

### Long waits: 30 minutes, and what it costs

Waiting is a first-class case — a nightly batch, a report build, an
overnight job. `DEMO-006` is the worked example; the pattern is one wait,
not a polling loop:

```python
JOB_SECONDS = 1800

def run(page, ctx):
    page.goto(f"{ctx.base_url}?timer={JOB_SECONDS}")
    page.click("#timer-btn")

    with ctx.timed("job wait"):            # shows on the step-timing chart
        page.wait_for_selector("#timer-done:not(.hidden)",
                               timeout=(JOB_SECONDS + 60) * 1000)

    assert "finished" in page.inner_text("#timer-done").lower()
```

Two limits have to allow it, and they are separate:

| limit | where | why |
|---|---|---|
| `timeout_seconds` | the sidecar / test edit form | the harness kills the run at this point. `0` means "use `runner.default_timeout_seconds`", which is **300** — so a 30-minute test MUST set this |
| the Playwright `timeout=` on the wait | your test code | Playwright's own default is 30s; a long wait has to say so explicitly |

**The continuous video is off by default (since 2.17).** The `.webm` grows
with wall-clock time whenever the page repaints, and a countdown repaints
every second. The measured 30-minute run above produced a **76.6 MB video**
— of a clock — against 4.9 MB of reel. Recording cannot be paused (a
Playwright constraint), which is exactly why the reel exists, and why the
reel is now the run's picture. Turn video on for the whole hub in
**Settings → Record video** (`runner.video`), or for one test with **Video
recording → always record** on its edit form (`"video": "on"` in the
sidecar); `"off"` keeps it off even when the hub records. A hub whose
`config.json` still says `"video": true` keeps recording until that box is
unticked. **`testhub purge_videos`** reports how much space the videos
already stored take, and `--delete` removes them — from disk and from S3 —
and nothing else (frames, screenshots, trace, log and history stay).

Measured, same 100-second job each time:

| | frames | reel | video | total |
|---|---|---|---|---|
| before idle sampling | 51 | 2.07 MB | 4.40 MB | 6.64 MB |
| idle sampling | 13 | 0.53 MB | 4.38 MB | 5.07 MB |
| + `"video": "off"` | 12 | 0.49 MB | — | **0.66 MB** |

and the real 30-minute job: **119 frames / 4.9 MB of reel, 76.6 MB of
video** — so turning video off is the difference between 82 MB and 5 MB
per nightly run. The run still shows its outcome, its plain-language
steps, the timed wait on the trend chart ("30 minute job wait = 30.0 min")
and a scrubbable replay ending on the finished job.

The demo page has a **Long-running job** button for trying this: it counts
down from 30 minutes by default, and `?timer=<seconds>` shortens it — so
`/demo/?timer=30` gives a 30-second version for a quick look.

On the finished-run page the replay and the **step timings** sit side by
side and share one clock: each step is listed at the moment it **began**
(`+0.72s`, `+1:02.35`), the same way the replay prints its time. While the
replay plays or is scrubbed, the step running at that moment is highlighted
(and any `ctx.timed()` block around it); **click a step** — or focus it and
press Enter — and the replay jumps to it. Frames are saved only when the
page changes, and at most every 0.4 s, so very quick steps can share one
picture. TypeScript runs follow the replay the same way: their start
times come from the Playwright trace the reel is extracted from. (A
TypeScript run from before 2.21, or one that kept no trace, lists its steps
without the sync.)

The page also carries the Playwright **trace** (`playwright show-trace
trace.zip` gives full time-travel debugging), your `ctx.screenshot()`
shots, the automatic failure screenshot and the log — and, for a hub or
test that records one, a link to the video.

**Files the test saved** are listed there too, under *Saved by the test*:
anything a test writes into `ctx.artifacts_dir` — a downloaded CSV, a PDF, a
report (DEMO-014's route plan, DEMO-022's accessibility report, DEMO-024's
label). HTML, SVG and XML files open **sandboxed** (`Content-Security-Policy:
sandbox`): a test often saves what the site under test gave it, and such a
file must never run that site's scripts with the hub's own session.

Artifacts live under `data/results/<TEST-ID>/<timestamp>-r<run>/` — plain
files, so they can be backed up or copied like anything else.

---

## The terminal — full control from any SSH session

Every hub since v2.11.0 can be driven entirely from a shell — useful
when the web port is still waiting on a firewall ticket, or over SSH.
The commands **hand work to the serving hub and exit**; anything you
start or schedule keeps running after you log out, because the systemd
service executes it, not your terminal.

```bash
testhub tui                            # full-screen dashboard (curses):
                                       #   r run  a all  k kill  w log
                                       #   s schedule  e edit  n/N new  R rescan
testhub run PAY-001                    # queue and EXIT -- go home
testhub watch                          # follow the newest run live
                                       #   (Ctrl+C stops watching, never the run)
testhub kill 42                        # same kill path as the UI button
testhub schedule PAY-001 "mon 02:00"   # saved in the sidecar -> travels with
testhub schedule PAY-001 "daily 14:30" #   the file; fires inside the service
testhub schedule                       # list all schedules
testhub list | stats | history PAY-001 | status
testhub new PAY-002 "Refund flow"      # scaffold (add --ts for TypeScript)
testhub edit PAY-002                   # $EDITOR (vi), then auto-rescan
```

`testhub runtest ID` (the old way) still runs synchronously in YOUR
terminal with full artifacts — handy before the service exists; it never
touches other runs. If `testhub run` warns "hub is not serving", the
queued work simply waits: start the service and it is adopted.

## For non-technical testers

The UI is meant to be usable end-to-end without reading code:

- **"What this test does"** on every test page — the code translated into
  English, one line per step. It understands both styles: `page.click("#go")`
  and the locator style the Playwright docs teach
  (`page.locator("#go").click()`, `page.get_by_role("button", name="Save")`),
  which is what most pasted-in or recorded code looks like. Waits state their
  limit in words ("Wait until #result appears (up to 1.5 min)").
- **Durations read as durations** — a 30-minute run shows as `30m 02s`
  everywhere, never `1802.4s`.
- **Help page** (in the nav) — a 5-minute guide living inside the hub:
  how to run, how to record with point-and-check, what every status word
  means, and what to do when something goes red. It works offline, which
  is the point.
- **“What this test does”** — every test page shows its steps in plain
  language (“Type ‘Ada’ into #name → Click #submit → Check #result says
  ‘Saved’”), with the code tucked behind a toggle.
- **“What went wrong”** — a failed run leads with the human part of the
  error next to the screenshot of that exact moment; the full traceback
  is behind *Technical details*.
- **Download report** — one self-contained HTML file (outcome, plain
  error, step timings, screenshots embedded) to attach to an email or
  ticket. No hub access needed to read it.
- **Suggested test IDs** — typing a name fills a valid ID automatically.

## Load & performance testing

Take a test you already have and run it **over and over, several copies at a
time, for a fixed stretch of wall-clock time**. Nav → **Load**.

Every copy is a **real browser** driving the real application, so what comes
back is what a user would actually feel — including *which step* got slow,
which is the part a requests-per-second number cannot tell you.

### What this answers, and what it does not

| question | the right tool |
|---|---|
| "can the server take 5,000 requests a second?" | JMeter / k6 / Locust — protocol-level, thousands of virtual users, no browser |
| **"does the app still work, and still feel fast, when 20 people use it at once?"** | **this** — real browsers, real JavaScript, real waits |

A handful of real browsers finding that the dashboard takes 9s instead of
1.2s tells you something no request count will. If you need raw protocol
throughput, use JMeter and keep this for the end-user experience.

### The plan comes from a real run

You cannot sensibly say "run it for 10 minutes" without knowing how long one
pass takes, so the page is baseline-first:

1. **Pick a test** — anything that already passes on its own.
2. The hub reads that test's **own recent history** and shows the median
   duration (if it has never run, it says so and sends you to run it once).
3. You choose a **window** and how many **simultaneous users**, and it
   projects the iteration count before you commit anything.

```
one pass takes 1.0s  ·  4 users  ·  10 minutes   ->  about 2,400 passes
```

**The projection is a prediction, and the gap is the finding.** A real count
well under it means the application slowed down under load — which is what
you ran it to discover. In the shipped demo below: 352 projected, 182
actual, because each pass went from 1.0s to 1.4s.

### Settings

| setting | what it does |
|---|---|
| **window** | how long to keep applying load (wall clock) |
| **simultaneous users** | how many copies run at once — each is a real Chromium |
| **ramp** | start them gradually over N seconds, so you can see *where* it starts to hurt instead of everything hitting at t=0 |
| **think time** | pause between passes, imitating a human reading the page |
| **stop after N passes** | a cap; 0 means "only when the time is up" |
| **keep artifacts** | off by default — see below |

**Leave "keep artifacts" off** unless you are debugging one specific failure.
Hundreds of videos of the same test is gigabytes of disk to repeat what the
timings already say, and the recording overhead distorts the very numbers you
came for.

### How many users can this machine take?

A virtual user is a **real Chromium**: roughly 250–400 MB of RAM and a chunk
of a CPU core. The Load page shows this machine's honest ceiling, computed
from its actual RAM and CPU count, and the planner warns when you exceed it.

> A load test that makes the machine swap is measuring the swap, not the
> application. The ceiling is not a target.

### What you get back

- **Percentiles, not an average** — p50 / p90 / p95 / p99. An average hides
  the tail, and the tail is what people complain about.
- **Slowdown factor** — median under load ÷ the same test on its own. "1.4×"
  is a sentence anyone can act on.
- **Error rate** — passes that failed outright once things got busy.
- **Throughput** — completed passes per minute.
- **Response time over the run** — the chart that earns the feature. Flat
  means the app shrugged it off; climbing shows where it started to hurt.
- **Which step got slow** — per-step timings aggregated across every pass,
  sorted by p95. *"The login POST is fine, it is the dashboard render that
  falls over at 8 users"* is actionable; *"p95 went up"* is not.
- **Slowest passes and failures**, each linking to its own run page.

### Load runs stay out of the test's own history

A 10-minute load run adds well over a thousand executions. If those counted
as ordinary runs, one load test would bury the test's real history — its
duration chart would become load data and the **ETA shown for a normal run
would become the loaded time**. (Measured before this was fixed: 99% of the
demo test's runs were load iterations and its estimate had drifted from
1.02s to 1.38s.)

So load iterations are deliberately excluded from:

- every chart, pass rate, p95 and ETA on the test page and Metrics
- the test page's run table and its "last run" chip
- the **Runs** list — add `?load=1` to see them
- the dashboard and the nav badge: a load test is **one** activity with its
  own row, not one row per virtual user

They are never orphaned — each iteration still has its own run page, reached
from the load run's *slowest* and *failures* tables.

The same reasoning applies to the **baseline**: it only ever counts runs made
on their own. Otherwise the second load test would compare loaded against
loaded and cheerfully report "1.0× slower".

### Stopping one

**Stop applying load** halts new passes. Copies already in flight are left to
finish on their own — cutting them off mid-flight would poison the very
timings being collected, and they are seconds from ending anyway.

### Plans travel with the test

A load plan is a *definition*, so it lives in the test's `.json` sidecar
alongside its tags and schedules — which means a plan designed on your laptop
arrives in the lab on the same disc as everything else:

```json
"load_plans": [
  { "name": "Ten minute soak (4 users)",
    "duration_seconds": 600, "concurrency": 4, "ramp_seconds": 60,
    "think_time_seconds": 0, "max_iterations": 0, "keep_artifacts": false }
]
```

The file is the source of truth: delete a plan from the sidecar and it goes;
edit the numbers and it updates in place, keeping its past load runs.

### The worked example

`DEMO-007` is a short form round-trip against the built-in demo page, with
each phase in its own `ctx.timed()` block so the step table has something to
show. It ships with two plans:

| plan | what it is for |
|---|---|
| **Quick smoke (90s, 2 users)** | check the setup works before committing ten minutes |
| **Ten minute soak (4 users)** | the real thing: 4 browsers, ramped over the first minute |

Measured on a developer laptop, the 90-second version:

```
baseline (alone)   1.015s
p50 under load     1.415s      1.39x slower
p95                1.536s
completed          182 of 352 projected
failures           0
throughput         119 passes/minute
```

The timeline shows exactly the expected shape — 1.02s while the ramp is still
filling, climbing to ~1.45s once all four users are working, then flat. That
plateau is the app finding its level; a line that keeps climbing is the one
to worry about.

---

## Keeping the machine awake

A machine that suspends at 01:50 does not run the 02:00 schedule — and the
symptom is *"the scheduler is broken"*, not *"the box went to sleep"*. On a
lab machine you cannot easily walk up to, that is a bad afternoon.

There is **no `caffeinate` on Linux**, so the base bundle ships one. Same
name, same idea, built on what RHEL 8 and CentOS 7 actually have
(`systemd-inhibit`, `setterm`, and `xset` when a desktop is present):

```bash
caffeinate                     # hold it awake until Ctrl-C
caffeinate -t 3600             # for an hour
caffeinate -- ./nightly.sh     # only while that command runs
caffeinate --status            # what would put this machine to sleep?
caffeinate --check             # exit 0 if it cannot suspend (for scripts)
```

Also reachable through the launcher: `testhub caffeinate --status`.

**`caffeinate -- <command>` always runs the command**, even when no
inhibitor lock can be taken. That is deliberate and was a real bug during
development: with no reachable `logind`, `systemd-inhibit` exits non-zero and
runs nothing, so wrapping a nightly job in it silently stopped the job. A
wrapper that swallows your work is far worse than one that fails to keep the
screen on.

**Who may hold the lock** — a *block* inhibitor is polkit-gated: **root
always may** (which is why the hub's keep-awake works — the service runs as
root), a desktop session usually may, and hardened machines **deny non-root
SSH sessions**. If `caffeinate` reports it could not take the lock over SSH,
that is the machine's policy, not a broken install: use `sudo caffeinate …`,
and remember the wrapped command ran regardless.

### From the app

The hub does this for you. While **anything is running** it holds a sleep
inhibitor, and releases it as soon as the last run finishes — reference
counted, so ten parallel runs share one lock and an idle hub still lets a
laptop sleep. Toggle: **Settings → Keep this machine awake while tests run**
(`runner.keep_awake`, on by default).

It is deliberately *not* held for the whole life of `serve`: a hub that is up
but idle has no business keeping a machine awake all week.

`testhub doctor` reports whether a lock can be taken at all. A Mac (the
laptop edition) has no such lock; there doctor says so, and how to keep the
Mac awake for unattended schedules.

### What it cannot do

An inhibitor stops *logind-initiated idle suspend*. It does not override a
BIOS timer, a hypervisor pausing the VM, or somebody running
`systemctl suspend`. `caffeinate --status` tells you whether the machine can
suspend in the first place, which is the thing worth knowing.

**For a lab server that must run overnight, do it once and permanently**
(needs root, survives reboots):

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

After that `caffeinate --status` reports *"sleep is masked on this machine —
it cannot suspend"* and scheduled tests cannot be missed for this reason.

---

## Security observation (DevSecOps)

Every test run also **watches how the application is configured** — the
response headers it sends, the flags on its cookies, and whether the page
tries to fetch anything from another host. Nav → **Security**.

It costs nothing extra: the browser is already there, already loading the
real application, already seeing all of it.

### Be clear about what this is

> **This is not a penetration test and not a vulnerability scanner.** Nothing
> here attacks the application, tries payloads, fuzzes inputs, or knows
> anything about CVEs. It reports what the browser saw. An empty list means
> *"no misconfiguration was visible from here"* — never *"this application is
> secure"*.

That distinction is printed on the page itself. A green tick that overstates
what it checked is worse than no tick at all.

What it does catch is **configuration mistakes**, which are the majority of
real findings in practice, and it catches them on every run without anyone
writing a test.

### What it looks for

| finding | severity | why it matters |
|---|---|---|
| **requests to other hosts** | high | see below — the important one here |
| mixed content (HTTP on an HTTPS page) | high | browsers block it, and it undoes HTTPS |
| a stack trace rendered in the page | high | leaks paths, versions, sometimes credentials |
| missing `Content-Security-Policy` | medium | the strongest single defence against XSS |
| missing `X-Frame-Options` / CSP `frame-ancestors` | medium | clickjacking |
| missing `Strict-Transport-Security` (on HTTPS) | medium | lets a browser be talked back to HTTP |
| session cookie without `HttpOnly` / `Secure` | medium | how session theft usually happens |
| plain HTTP | medium | flagged, but the fix text says an isolated lab may mean it |
| `Server` / `X-Powered-By` version banners | low | tells an attacker what to look up |
| weak `SameSite` | low | part of CSRF defence |
| a **CSRF token** cookie readable by JS | info | *expected* — see below |

**Accuracy over volume.** A CSRF token cookie *must* be readable by the page's
own JavaScript, so it is reported as **informational and expected** rather
than as a defect — telling a tester to "fix" it would break the application.
Likewise a modern CSP with `frame-ancestors` satisfies the clickjacking check
on its own; demanding `X-Frame-Options` as well would be noise.

### The one that matters most on an air-gapped network

**Any request leaving for a third-party host** is reported as high severity.
On an isolated network it cannot succeed — so the page is either broken or
sitting through a timeout — and each one is a route by which data could leave
the enclave. A browser sees these directly; nothing else in the toolchain
does. The Security page lists every external host observed, with counts.

### Where it shows up

- **Security** page — current posture, grouped by *problem* rather than
  repeated per test ("no CSP" seen by nine tests is one problem with one fix),
  worst first, built from the **newest run of each test** so a finding you
  fixed stops being reported.
- **Each run page** — what that run saw, with what to do about it.

Switch it off with `runner.security_checks: false` if you ever need to.

### Testing behind a login

Most of an application is only reachable once you have signed in, so the
checks that matter most need a session.

**Credentials never go in a test file.** Test files travel — laptop, zip,
disc, git — and a password written into one travels with all of it. Instead:

1. **Settings → Credentials** — add a name and a value. Stored in
   `data/secrets.json` at `0600`, on this machine only.
2. In the test, refer to it **by name**:

```python
page.fill("#password", ctx.secret("target_password"))
```

The name travels with the test; the value stays put. The same test then runs
on your laptop and in the lab as soon as each machine has its own value —
which is also correct, because the lab password should not be the laptop
password. A missing secret fails loudly rather than submitting an empty
string and failing later somewhere confusing.

**Values are redacted from everything.** `run.log`, `result.json`, error
messages and the stored failure text all pass through a filter that replaces
known secret values with `***`. Verified with a test that deliberately leaked
its password four different ways (a log line, a bare `print`, typing it into
the page, and an assertion message): **zero occurrences** survived anywhere.
Credentials are also excluded from backups and from the tests export zip.

### Checks that need a session

Wrap the flow and the hub observes it. None of these attack anything — they
watch a flow the application performs anyway, or repeat a request it already
serves.

```python
def run(page, ctx):
    page.goto(f"{ctx.base_url}login/")

    with ctx.logging_in():                  # is the session regenerated?
        page.fill("#username", "tester")
        page.fill("#password", ctx.secret("target_password"))
        page.click("#login-btn")
        page.wait_for_selector("#private-heading")

    ctx.check_protected_page()               # does it really need a login?

    with ctx.logging_out():                  # does logout actually invalidate?
        page.click("#logout-btn")
```

| check | what it catches | severity |
|---|---|---|
| `ctx.logging_in()` | **session fixation** — the identifier issued *before* you authenticated is still in use afterwards, so anyone who knew it now holds a logged-in session | high |
| `ctx.check_protected_page()` | a private page served to a caller with **no session at all** | high |
| `ctx.logging_out()` | the old session cookie **still works** after signing out — logout cleared the browser but not the server | high |
| (automatic) | a logged-in page without `Cache-Control: no-store`, readable by the next person on a shared workstation | low |

A redirect to the login page, a `401` or a `403` are all **correct** answers
and are not reported. That distinction cost a real bug during development:
Playwright follows redirects by default, so the first version saw the login
page's own `200` and reported two false "holes". Requests are now made with
`max_redirects=0`.

`DEMO-008` is the worked example, against the demo target's own login — which
is implemented *correctly*, so it shows what a clean result looks like. Add
`?weak=fixation` to the demo login URL to see the failing case on purpose:
that flow skips the session regeneration and is reported as high severity.

### Supply chain: the SBOM

An air-gapped machine **cannot** be scanned for known vulnerabilities — there
is no advisory feed to consult, and a database burned onto the disc is stale
within weeks. Shipping a scanner that reports "0 vulnerabilities" from a
six-month-old database would be worse than shipping nothing.

So the kit carries a **Software Bill of Materials** instead, one per OS:

```
dist/labkit/SBOM-rhel8.10.json     522 components
dist/labkit/SBOM-centos7.9.json    459 components
```

CycloneDX 1.5, listing every Python package, system RPM, browser build, Node
and the interpreter, each with a `purl` that scanners match on. Carry that one
small file to a machine that *does* have a feed and scan it with whatever your
organisation already uses — Dependency-Track, grype, trivy, osv-scanner all
read CycloneDX. Many government programmes require an SBOM as a deliverable
anyway.

The SBOM is generated from the **finished zip**, so it describes exactly what
shipped, and it is covered by the kit's `SHA256SUMS`. It is also
byte-reproducible — no timestamps or random serials — so a diff between two
releases shows what actually changed.

Regenerate one at any time:

```bash
python3 scripts/make-sbom.py dist/offline-bundle-rhel8.10-py3.9.25.zip rhel8.10 sbom.json
```

### Feeding it to Dependency-Track

If your organisation runs Dependency-Track, upload the SBOM **from the office
side** — the lab machines have no route to it and never will:

```bash
export DTRACK_API_KEY='...'          # a key with BOM_UPLOAD permission
scripts/upload-sbom.py https://dtrack.example.gov \
    dist/labkit/SBOM-rhel8.10.json \
    --project pw-offline-rhel8 --version 2.8.0
```

Dependency-Track matches every component's `purl` against its own mirrors of
NVD, OSV and the GitHub advisories, **and keeps doing so** — a package that
becomes vulnerable next month shows up without anyone re-uploading anything.
That continuous part is the reason to wire it up rather than run a one-off
scan.

The API key is read from the environment only. It is never accepted as a
command-line argument, because arguments are visible in `ps` to every user on
the machine. Standard library only, so it runs anywhere `python3` does.

Upload one project per OS (`pw-offline-rhel8`, `pw-offline-centos7`) and use
the bundle version as the project version, so Dependency-Track can show you
what changed between two kits.

---

## Metrics — what you get and what it means

**Step timings** — every run records how long each page action took
(automatic) plus your `ctx.timed(...)` blocks. The run page shows them as
a bar list; the test page adds **Step timing trends**: the same step
matched by name across the last runs, one line per step, slowest first —
so "the login click got 400ms slower since Tuesday" is a glance, not an
investigation.

**Per test** (test page): pass rate; total runs; average / p50 / p95
duration; current streak; **flakiness** (share of adjacent pass/fail runs
that flip — 0 stable, 1 alternates every run); the last-60-results strip;
**duration-per-run** chart (each point one run, coloured by outcome, with a
7-run rolling average — click a point to open that run); **outcomes by
target version** (the "did 2.4 break this?" chart); filterable run table.

**Global** (*Metrics* page, 7–180-day window): runs/day stacked by
outcome; pass-rate trend; top-10 slowest (avg + p95); top-10 flakiest;
outcomes by version; what-starts-the-runs split (manual / selection /
group / schedule); total machine time spent testing; a per-test summary
table — plus, added for triage:

- **Started failing recently vs. long-standing** — a fresh regression is
  a different problem from a known-broken test; the dashboard also
  surfaces the "started failing" list on top.
- **Failures grouped by cause** — failures clustered by their (noise-
  normalised) message, with the tests each one affects. One app bug
  breaking five tests shows as ONE row, which is usually the first thing
  worth knowing.
- **Slowest individual steps** across the whole suite — not just slow
  tests but the exact actions/waits burning the time.
- **When failures happen** — a weekday × time-of-day heatmap; a stripe
  means an environment problem (deploy window, backup) rather than a test
  problem.
- **Suite health** — never-run, not-run-in-14-days and disabled tests, so
  the test set itself cannot rot silently.
- **CSV export** of the per-test table for sharing numbers with people
  who have no hub access.

**Per test, additionally**: a **duration histogram** (an average hides
"usually 3 s, sometimes 40 s" — the histogram doesn't) and **why this
test failed**, its own failures grouped by message. The tests list shows
an inline **sparkline** per test: last 16 runs, bar height = duration,
colour = outcome. Group pages show a **pass-rate-over-time** curve across
that group's batches — the release-readiness view.

**Dashboard**: live *Running now* panel (status, elapsed, ETA-or-null,
progress, kill), recent batches, next scheduled runs, failing-now, 14-day
run history.

---

## The disk workflow (personal computer → work)

Two kinds of update travel on the disk, and they are deliberately
independent:

**A. Tests changed (the common case — no reinstall at all).**
On the work machine, copy new/changed `.py`+`.json` pairs (and
`_groups.json`) into `/opt/pw-testhub/data/tests/`, then press *Rescan* or
run `testhub sync`. Done. Removing a file archives its test (history
kept); putting it back restores it.

**B. The app itself changed (new features / fixes).**
On the personal computer: bump `app/VERSION`, run `make bundle-app`, carry
`dist/pw-testhub-app-<ver>.zip` across, and on the work machine run
`sudo ./install-app.sh` from the unzipped folder. That singular command is
the whole upgrade: code replaced, wheels reinstalled, DB migrated, tests
re-synced — and `data/` + `config.json` are never touched. Previous code
stays at `/opt/pw-testhub/app.old` for rollback.

The **base** bundle (Python/Playwright/browsers) keeps its own existing
flow (`make bundle-rhel8` etc.) and changes rarely — only for new
browsers, Python packages or system libs. After re-running the base
installer on a target, re-run `install-app.sh` too (its venv was
recreated; the app zip's wheels are also cached at
`/opt/pw-testhub/wheels` for exactly this).

DB compatibility across app upgrades is handled by Django migrations —
ship schema changes as migration files inside the app zip and the
installer applies them.

---

## Disk space: what to record, what to delete, where to keep it

Three independent levers, all on the **Settings** page.

**1. Record less.** Checkboxes control what the hub saves on every run:

| switch | rough cost per run | notes |
|---|---|---|
| Video | ~1–5 MB per minute | the biggest item by far |
| Activity-replay screenshots | ~30–60 KB per captured frame | idle time costs nothing (no paint, no frame) |
| Playwright trace | ~0.5–3 MB | the deep-debug artifact |
| Live view | negligible | one file, overwritten |

Your own `ctx.screenshot("name")` calls are always saved — these switches
only govern automatic recording.

**2. Delete what you have.** Two distinct levels, and the distinction
matters:

- **Delete artifacts** — videos/screenshots/traces go, the run rows stay.
  Reclaims essentially all the space while every chart, pass rate,
  duration and timing survives. This is what you normally want.
- **Delete runs entirely** — rows go too; charts lose those runs.

Where: a **Delete files** button on any run page and on any test page
(with a "keep the newest N" prompt), and on Settings a bulk *Free up
space* card — delete older artifacts across all tests, delete only
*passed* runs' artifacts (failures are usually worth keeping), or delete
runs entirely. Automatic: `maintenance.retention_days`.

**3. Keep it somewhere else (AWS).** `storage.backend: "s3"` uploads each
finished run's artifacts to a bucket, then optionally deletes the local
copy — so the instance disk stays flat no matter how long history grows.
The **database always stays local** (small, and every chart reads it).
Run pages then serve artifacts through short-lived presigned URLs; the
file list recorded at upload time means no S3 call just to render a page.

```jsonc
"storage": {
  "backend": "s3",
  "bucket": "my-testhub-artifacts",
  "prefix": "testhub",
  "region": "us-east-1",
  "delete_local_after_upload": true,
  "url_expiry_seconds": 3600
}
```

Credentials come from the **instance role** (or the environment / `~/.aws`)
— never from config.json. Settings has *Test the bucket* and *Upload
existing artifacts now* (for the backlog from before you switched it on).
If the bucket is unreachable, a run keeps its artifacts locally and logs
the reason: storage trouble never loses a run. Offline machines leave
this on `local`, which is the default and requires nothing.

## The activity strip (top of every page)

Whenever anything is **running**, a strip appears under the top bar — on
**every** page, so nobody has to find the dashboard to answer "why is the
machine busy?". It shows what is running right now (since 2.20 the queue
lives on the dashboard's *Running now*, where a waiting run can still be
cancelled):

- **a group run** — ONE entry: the group's name (click to open its batch
  page), which of its tests are running this moment, how many are done,
  the time left for the whole group, and a ✕ to kill the group run;
- **a running test** that is not part of a group run — test id + name
  (click to watch it live), elapsed time and estimated time left, and a ✕
  to kill it;
- **load runs** — one entry per load run (never one per virtual user),
  with browsers in flight and time remaining, and a ✕ to stop applying
  load (passes already in flight finish, so the timings stay honest).

More than six items collapses into "+N more →" linking to the dashboard.
When nothing is running the strip disappears entirely.

**The rest of the page keeps up too (since 2.18) — without reloading.**
The same poll notices when a run is queued, starts or finishes, and the
page's run data is fetched again in the background and swapped in place:
the dashboard's tiles and charts, the tests and runs lists, a test's
history and charts, a batch's results, the Groups and Schedules histories,
Analytics, Metrics, Security and the load-test lists. While anything runs
it also refreshes every 10 s. Your scroll position stays where it is, and
nothing you are working with is overwritten: a part of the page holding a
focused field or selected text waits for the next update, and ticked
checkboxes (the tests list's selection) stay ticked. A run page you are
watching swaps its live view for the finished results the moment the run
ends; after that it stays still, so a replay you are scrubbing never
resets. Buttons that used to reload the page (Rescan, schedule
Pause/Delete, Delete files) update it the same way.

## Analytics — everything by website version

Set **`target.version`** in Settings to the build of the website you are
testing; every run is stamped with it at the moment it runs. (Testing the
built-in demo, runs are stamped with its deployed release automatically —
Acme Freight's five releases are the ready-made way to see this page work.)
The **Analytics** page then answers the release questions Metrics cannot:

- pass rate, volume and median duration **per website version**;
- **every test × every version** — a version that broke or slowed one
  test shows up instead of averaging away;
- **speed trend**: each test's median on the current version vs the one
  before, sorted worst-first.

Every table doubles as a **Copy-as-CSV text block** (always including the
test name and whether it passed or failed). That is deliberate: on a
network where the chat tool can carry text but not files, you copy a
block, paste it into chat, and save the paste as the suggested `.csv`
name on the other side. Clicking a block selects all of it; the Copy
button works on plain http.

## Setting the machine's clock

Air-gapped machines drift — no NTP ever reaches them — and a wrong clock
fires schedules at surreal hours and scrambles "last run" times.
**Settings → Machine clock** shows this machine's time next to your
device's with the skew spelled out, and one button sets the machine to
your device's clock (a manual picker is beside it). Needs the hub running
as root (the systemd service does); under the hood it is
`timedatectl set-ntp false` + `set-time`, so the fix sticks.

## When something is wrong: `doctor`

    /opt/pw-testhub/bin/testhub doctor          # full check (launches a browser)
    /opt/pw-testhub/bin/testhub doctor --quick  # skip the browser launch

One command answers "why isn't this working?" — Python, Playwright,
bundled browsers, an actual browser launch, SQLite version, pending
migrations, folder permissions, free disk, timezone validity, whether a
hub is serving and running tests (from the terminal, doctor asks the
running hub; it has no runner of its own), and target reachability —
each with a sentence saying what to do about it. Exit code 1 if anything failed, so it can gate a script. The
same checks appear on the **Settings** page (with a button to re-run the
slow browser-launch check).

Two checks exist only where they can matter, and stay silent elsewhere:

- **fapolicyd** (hardened RHEL 8): whether the daemon is enforcing and the
  runtime's allow-list (`/etc/fapolicyd/rules.d/10-pw-offline.rules`) covers
  the install paths. The failure it catches reads as a broken runner —
  every *new* test run dies with "operation not permitted" while the hub
  itself keeps serving. The fix it names: re-run the kit's
  `install-all.sh` (no zips needed).
- **OpenCode**: whether the bundled AI agent is installed, and the exact
  `source /opt/pw-offline/env.sh` line that puts it (and playwright, and
  robot) on a shell's PATH — a mistyped source line fails silently and
  looks like a missing install.

## Backup, restore and moving tests between machines

| what | how | contains |
|---|---|---|
| **Backup** | Settings → *Download backup*, or `testhub backup` | tests + run history (DB) + config.json |
| **Restore** | `testhub restore <file>` (`--tests-only` keeps this machine's history) | — |
| **Export tests** | Settings → *Download tests*, or the export URL | test definitions only |
| **Import tests** | Settings → *Import*, or `testhub import_tests <zip>` | test definitions only — run history is never touched |

Safety rails: a backup is taken automatically before an import or a
restore; anything replaced is **moved aside** (`data/_replaced-<stamp>/`),
never deleted; the installer takes a **pre-upgrade database copy** before
running migrations (last 10 kept in `data/backups/`); and imports refuse
anything that is not a plain `.py`/`.json` test file (no paths, no
traversal, invalid JSON rejected).

The tests zip is the disk-transfer format: export on the laptop, carry,
import at work — safer than copying files by hand because it validates
and re-syncs for you.

## Locking the hub down (optional)

`site.password` in config.json turns on a single shared password with a
sign-in page. Everything is then protected — pages, APIs and
mutations — except the sign-in page, static files, and `/api/health/`,
which answers only "alive" so a load balancer can probe it without
leaking test names or activity. It is deliberately not user accounts: on AWS put ALB
authentication in front instead (docs/AWS.md), and in a lab restrict the
network. Empty (the default) = no gate. Set it, `chmod 600 config.json`,
restart.

## Operations notes

- **Backup** = copy `/opt/pw-testhub/data/` (tests, results, DB) and
  `config.json`. Restore = put them back, start the hub.
- **Disk growth**: run artifacts (videos, traces, frames) are the only
  thing that grows without bound. The Settings page shows current usage
  and offers **prune artifacts older than N days** (a button now, or
  `maintenance.retention_days` in config.json for automatic pruning at
  startup). Pruning deletes only artifact folders — the database history
  and every chart survive; old run pages just have no video.
- **Target reachability**: Settings → *Check target URL now* proves the
  hub machine can reach the site under test — the first thing to verify
  on a new deployment.
- **Archived tests** can be restored with one click on the Settings page
  (moves the files back from `tests/_archive/` and re-syncs).
- **Duplicate** on a test page starts a New Test form prefilled with that
  test's code and metadata — the quickest way to write variants.
- **Logs**: the serve console (or `journalctl -u pw-testhub`); per-run logs
  in each run's artifacts folder.
- **Crash recovery**: runs left "running" by a crash/reboot are marked
  `aborted` at the next startup — they never hang the queue. A *clean* stop
  (Ctrl-C, `systemctl stop`) does not need this: it terminates in-flight
  runs itself and marks them before exiting.
- **`manage.py runtest` is safe to run while the hub is serving.** It
  deliberately skips the orphan recovery that `serve` does at startup —
  that recovery assumes nothing else is executing, and running it here
  aborted every run the live server had in flight (measured: one CLI
  invocation killed two).
- **Purging is bulk.** Clearing run data is two SQL statements regardless
  of how many runs are involved, and the Settings page's disk-usage figure
  is cached for two minutes (it walks every artifact file, which is slow
  once there are tens of thousands).
- **Headed runs on servers**: set `runner.headed: true` only where a
  display exists; on a headless box use `xvfb-run -a bin/testhub serve`
  (the base bundle ships Xvfb).
- **CentOS 7**: the app needs a base bundle built after the SQLite fix
  (Django 4.2 requires SQLite ≥ 3.21; the base build now compiles 3.46
  into the prefix on that platform, exactly like it already did OpenSSL).
  The app installer checks and refuses with a clear message otherwise.
  RHEL 8 (SQLite 3.26) has no such requirement.
- **Version ceilings**: Django 4.2 LTS is pinned because it is the **last
  Django supporting Python 3.9** — same reasoning as Playwright 1.60 and
  rfbrowser 19.9 in the base bundle. Its upstream support ended 2026-04;
  acceptable for an air-gapped internal tool, and moving the fleet off
  Python 3.9 lifts every one of these ceilings at once.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| red "read-only mode" banner | started with `runserver` — use `manage.py serve` / `bin/testhub serve` |
| runs fail instantly, "browser launch failed" | env not sourced (use `bin/testhub`, which sources `env.sh`); or `--no-browsers` base install |
| ETA shows null | correct behaviour: that test has no pass/fail history yet |
| schedule did not fire | hub not running at slot time and past `catchup_minutes`; check timezone in Settings |
| live view empty | non-chromium browser, or the run finished; video/trace cover it after the fact |
| port already in use | another hub instance; change `site.port` or stop the other one |
| "database is locked" in logs | transient under heavy parallelism — WAL + busy-timeout retry it; if persistent, data dir is on NFS (put it on local disk) |
| killed run has no video | expected. The `.webm` is only finalised when the browser closes normally; a kill interrupts that. Use the **activity reel**, whose frames are written as the run goes |
| `[harness] ... the browser stopped responding` in a run log | normal on Kill — the driver cannot be reused after an interrupted call, so the remaining artifacts are skipped rather than waited on. The verdict and timings were already saved |
| a killed run takes ~12s to settle | the harness is being given time to write `result.json` first. Longer than that means the post-mortem steps are each timing out; the log names which |

## Design notes

UI colors follow a CVD-validated palette (the dataviz reference instance):
status green/red never sit adjacent in stacked charts (the neutral bucket
separates them), every status is always paired with its text label, charts
carry legends and table equivalents, and light/dark are both first-class.
Chart.js is vendored at `app/core/static/core/vendor/` — the UI makes zero
network requests, because the machines it runs on have no network.

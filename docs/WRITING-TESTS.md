# Writing tests for the Test Hub

Everything about turning "we should check that…" into a test the hub runs,
charts and schedules. It covers where files go, the Python and TypeScript
contracts, shared code, the settings file beside each test, groups,
credentials and results.

Paths below assume the demo's data folder `data/`. If you gave your site its
own folder (`"paths": { "data_dir": "data-portal" }` in `config.json`),
read `data-portal/` instead.

## Where everything lives

| what | where |
|---|---|
| test definitions (the source of truth) | `data/tests/` |
| shared code your tests import | `data/tests/_lib/` |
| results: one folder per run | `data/results/<test id>/<date-time>-r<run>/` |
| run history (feeds every chart) | `data/db.sqlite3` |
| credentials (never in test files) | `data/secrets.json` (private to your user) |
| this machine's settings | `config.json` next to `testhub.sh` |

**The files own the test definitions; the database owns the run history.**
Every edit in the web page writes the files back, and every start or
**Rescan tests folder** reads them again. That is why copying files, git
checkouts and zips all work.

After adding or changing files by hand: **Rescan tests folder** (Dashboard
or Settings), or `./testhub.sh manage sync`.

## Python or TypeScript?

Both are first-class: same runner, history, charts, schedules, groups,
analytics and load testing. Pick what your team writes.

| | Python | TypeScript |
|---|---|---|
| file | `ID__name.py` | `ID__name.spec.ts` |
| runs on | Playwright for Python 1.60 | `@playwright/test` 1.58.2 |
| entry point | `def run(page, ctx):` | one or more `test('…', async ({ page }) => …)` |
| every page action timed | yes | yes (from the run's trace) |
| named phases | `with ctx.timed("label"):` | `await test.step('label', async () => …)` |
| credentials | `ctx.secret("name")` | `process.env.PW_SECRET_NAME` |
| passive security observation | yes | no (Python only) |

The **id** is everything before `__` in the file name (`LOGIN-001`,
`CART-T01`); keep it unique. The rest of the name is free-form.

## Python tests

A test is a file exposing `run(page, ctx)`:

```python
def run(page, ctx):
    page.goto(ctx.base_url)
    assert page.url.startswith(ctx.base_url), f"landed somewhere else: {page.url}"

    with ctx.timed("sign in"):
        page.get_by_label("Username").fill("qa-user")
        page.get_by_label("Password").fill(ctx.secret("site_password"))
        page.get_by_role("button", name="Sign in").click()
        page.get_by_text("Welcome").wait_for()

    ctx.screenshot("signed in")
```

`page` is a normal Playwright `Page`: the whole
[Playwright for Python API](https://playwright.dev/python/docs/api/class-page)
works, and every call on it is timed automatically. A failed `assert`, or
any exception, fails the test, and its message is what the run page shows,
so make it say what went wrong.

`ctx` gives you:

| | |
|---|---|
| `ctx.base_url` | the target's URL (`target.url`). Never hard-code the site: the same test then runs against staging, the lab, or the demo. |
| `ctx.timed("label")` | a named, timed phase (`with` block), shown around the steps inside it and charted run after run |
| `ctx.secret("name")` | a credential from *Settings → Credentials*. Missing? The test stops with a message saying where to add it. `ctx.secret("name", "fallback")` gives a default instead; `ctx.has_secret("name")` asks. |
| `ctx.screenshot("name")` | a named screenshot kept with the run |
| `ctx.log("text")` | a line in the run's log |
| `ctx.artifacts_dir` | the run's folder. Files a test saves there (a download, a report) are listed on the run page. |
| `ctx.test_id` | this test's id |
| `with ctx.logging_in():` / `ctx.check_protected_page()` / `with ctx.logging_out():` | optional, passive security checks around your own login flow: is the session renewed at sign-in, does a protected page refuse visitors without one, does sign-out really end it? They show on the *Security* page. |

If the tests will also run on the air-gapped lab machines, keep to **Python
3.9** syntax (no `match`, no `X | Y` type unions); that is the Python there.

## TypeScript tests

A completely standard `@playwright/test` spec:

```ts
import { test, expect } from '@playwright/test';

test('sign in', async ({ page }) => {
  await page.goto('./');                       // RELATIVE: the target itself
  await test.step('sign in', async () => {
    await page.getByLabel('Username').fill('qa-user');
    await page.getByLabel('Password').fill(process.env.PW_SECRET_SITE_PASSWORD ?? '');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByText('Welcome')).toBeVisible();
  });
});
```

- **Navigate relative to the target**: `page.goto('./')`,
  `page.goto('orders/42')`. The hub sets `baseURL` to `target.url`, and
  `'/'` would mean the root of the *server*, which for a target under a
  path is somewhere else.
- A file may hold several `test()` blocks. The file is ONE test in the hub;
  each block shows as a phase of it.
- `test.step()` blocks become the named phases on the run page. Every page
  and API call and every web-first `expect` inside them is timed
  automatically.
- `expect.soft(…)` records a failure and carries on, so one run reports
  every broken promise (see `DEMO-T13`). The failure message on the run page
  lists them all with expected and received values.
- The `request` fixture tests an HTTP API with no browser at all (`DEMO-T13`).
- Credentials: a secret named `site_password` arrives as
  `process.env.PW_SECRET_SITE_PASSWORD` (upper case, anything not a letter
  or digit becomes `_`), and is blanked out of logs.
- Type-check before you run: `./testhub.sh typecheck` (strict mode).
- Browsers: `chromium` (default), or real Chrome/Edge channels where installed.

## Shared code (page objects, helpers)

Anything whose name starts with `_` is **ignored by the scanner** and
**importable by tests**:

```
data/tests/
  _lib/
    __init__.py          (Python: makes _lib a package)
    pages.py             page-object classes
    pages.ts             ...and their TypeScript twins
  LOGIN-001__sign_in.py
  LOGIN-T01__sign_in.spec.ts
```

```python
from _lib.pages import LoginPage

def run(page, ctx):
    LoginPage(page, ctx).open().sign_in("qa-user", ctx.secret("site_password"))
```

```ts
import { signIn } from './_lib/pages';
```

Working examples ship with the samples: `_lib/freight.py` and
`_lib/freight.ts` (page objects for the demo site), used by DEMO-010…024
and DEMO-T12…T16, and `_lib/pages.py` with DEMO-009.

## The settings file beside each test

Next to each test sits `ID__name.json`. The hub creates it with defaults
if it is missing, and the web page edits it. Every key:

```jsonc
{
  "id": "LOGIN-001",
  "name": "Sign in",                    // display name
  "description": "What a failure MEANS, for whoever is on shift.",
  "tags": ["auth", "smoke"],            // filterable labels
  "version": "1.2",                     // YOUR version of the test itself
  "timeout_seconds": 300,               // stopped after this long
  "enabled": true,                      // false: never scheduled, still runnable by hand
  "video": "",                          // "" hub default · "on" · "off"
  "load_plans": [                       // optional: load tests (Load page)
    { "name": "lunch rush", "duration_seconds": 600, "concurrency": 4,
      "ramp_seconds": 30, "think_time_seconds": 1.0 }
  ],
  "schedules": [                        // optional: runs by itself
    { "days": ["mon", "wed", "fri"], "time": "02:00", "enabled": true,
      "browser": "chromium" }
  ]
}
```

## Groups

A group runs several tests with one click or one schedule. They are
kept in `data/tests/_groups.json`, and the *Groups* page edits it:

```json
{ "groups": [ { "name": "Smoke", "description": "",
                "tests": ["LOGIN-001", "CART-001"],
                "schedules": [ { "days": ["mon"], "time": "02:00", "enabled": true } ] } ] }
```

## Credentials: the one rule

Test files travel (git, zips, disc), so **a password written in a test
travels with it.** Put values in *Settings → Credentials*. There they are
stored privately, left out of exports and backups, and blanked out of every
log. Refer to them by name. The name travels; the value stays on each
machine.

## Getting tests in and out

1. **Copy files** into the tests folder, then *Rescan*.
2. **Zip**: *Settings → Download tests* on one machine, *Import tests* on
   another (checked, carries `_lib/`, takes a backup first).
3. **Git**: the tests folder can be a git checkout of your own tests repo
   (`.git` is ignored by the scanner). This keeps tests reviewed and versioned
   like code.

New and changed files appear after a rescan. A removed file archives its
test: history is kept, and it can be restored from *Settings*.

## Running, and reading the results

- One test: its **Run** button. Several: tick them on *Tests* → **Run
  selected**. A group: *Groups* → **Run group**. On a timetable: *Schedules*.
- From the terminal: `./testhub.sh run LOGIN-001 CART-T01` (queued into the
  running hub), then `./testhub.sh manage watch <run id>`,
  `./testhub.sh manage history LOGIN-001`, `./testhub.sh manage stats`.
- Every run keeps its folder under `data/results/`: `run.log`,
  `result.json` (status + per-step timings), screenshots, the activity
  replay's frames, the Playwright `trace.zip` (time-travel debugging:
  `.venv/bin/playwright show-trace data/results/…/trace.zip`), and any
  files the test saved. *Settings → Free up space* deletes old artifacts but
  keeps every chart.

**Stamp the website's version** (*Settings → Target → version*) whenever a
new build is deployed. Every run records it, and *Analytics* then answers
"what did the new release break, or slow down?" test by test.

## Scaffolding from the terminal

```bash
./testhub.sh manage new LOGIN-001 "Sign in"          # Python
./testhub.sh manage new LOGIN-T01 "Sign in" --ts     # TypeScript
```

This creates a working starter test and its settings file in the tests
folder, already known to the hub. Edit it, then `./testhub.sh run LOGIN-001`.

## Recording instead of typing

*Tests → Record new test* opens the **Remote Recorder**. A browser runs on
the hub and is shown inside the page. Click through your site; right-click
anything to add a check ("should be visible", "should say…"); and stop. You
land on a new test, in Python or TypeScript, with every step written as
selector-based code that you can edit like any other test. You can also
record *into* an existing test (appended at the end).

## Tests that last

- **Selectors that survive a redesign**: ids, roles and labels
  (`get_by_role`, `get_by_label`, `#stable-id`), not CSS paths like
  `div > div:nth-child(3)`. The demo's tests pass on two completely
  different designs because of this.
- **Check where you are.** A sign-in page, an SSO redirect or a maintenance
  page also has a title and runs JavaScript. Assert the URL or a heading
  that only the real page has.
- **Wait for things, not for time.** `wait_for_selector`, `expect(…)`,
  `wait_for_url`; never a fixed `sleep`.
- **One user goal per test**, named for it ("customer can reorder from
  history"), with assertion messages that say what broke ("the total
  should be $42.00, the page says $84.00").
- **Independent tests.** Each creates or finds its own data, so they can run
  in any order and two at a time.
- **Run a new test several times** before you trust it. The *Metrics*
  page flags tests that flip between pass and fail.

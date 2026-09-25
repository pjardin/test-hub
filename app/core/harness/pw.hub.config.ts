import { defineConfig } from '@playwright/test';

/**
 * The Python hub's config for running TypeScript tests -- driven entirely
 * by environment variables set by run_ts_test.py, so the hub's per-run
 * settings (video, trace, timeout, target URL) reach @playwright/test
 * without touching any file in data/tests.
 *
 * Specs use RELATIVE paths (page.goto('./'), goto('freight/')): baseURL is the hub's
 * target.url. Secrets arrive as PW_SECRET_<NAME> environment variables.
 */
const trace = process.env.PW_TS_TRACE || '';

export default defineConfig({
  testDir: process.env.PW_TS_TESTS_DIR || '.',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: Number(process.env.PW_TS_TIMEOUT_MS) || 30000,
  use: {
    baseURL: process.env.PW_TS_BASE_URL || undefined,
    video: process.env.PW_TS_VIDEO ? 'on' : 'off',
    screenshot: 'only-on-failure',
    // 'full'   = the tester asked for a trace artifact
    // 'frames' = trace forced on ONLY to harvest its paint-triggered
    //            screenshots for the activity reel (snapshots skipped
    //            to keep it light); the adapter deletes the zip after
    trace: trace === 'full'
      ? { mode: 'on', screenshots: true, snapshots: true }
      : trace === 'frames'
        ? { mode: 'on', screenshots: true, snapshots: false }
        : 'off',
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'chrome', use: { browserName: 'chromium', channel: 'chrome' } },
    { name: 'msedge', use: { browserName: 'chromium', channel: 'msedge' } },
  ],
});

import { test, expect } from '@playwright/test';

// A TypeScript test running through the PYTHON hub: same demo target, same
// history/charts/schedules -- but executed by the node @playwright/test
// engine at /opt/pw-ts. baseURL is the hub's target.url, so relative
// page.goto() works here exactly like ctx.base_url does in Python tests --
// './' is the target itself. Never '/': that is the server's root, which
// for a target under a path (the built-in demo is /demo/) is somewhere else.
test('demo page loads and has its title', async ({ page }) => {
  await page.goto('./');
  await expect(page).toHaveTitle(/Demo/i);
  // same lesson as DEMO-001: assert we are still ON the target, so an SSO
  // bounce or a maintenance page cannot pass by also having a title
  await expect(page).toHaveURL(/\/demo\//);
});

test('the demo form round-trips a request', async ({ page }) => {
  await page.goto('./');
  await test.step('fill and submit', async () => {
    await page.fill('#name', 'ts through python hub');
    await page.click('#submit-btn');
  });
  await expect(page.locator('#result')).toContainText('ts through python hub');
});

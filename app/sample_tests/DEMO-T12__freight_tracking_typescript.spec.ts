import { test, expect } from '@playwright/test';
import { open, track } from './_lib/freight';

// Freight tracking in TypeScript -- the same checks as DEMO-010 (Python),
// written with Playwright Test's web-first assertions: expect(locator)
// retries until the page agrees, or the timeout says it never will.
test('tracking shows a shipment in transit, and says so for an unknown number', async ({ page }) => {
  await open(page);
  await expect(page).toHaveURL(/\/demo\/freight\//);        // really on the demo site
  await expect(page.locator('#hero-title')).not.toBeEmpty();
  console.log(`Acme Freight says it is release ${await page.getAttribute('meta[name="app-version"]', 'content')}`);

  await test.step('track a shipment in transit', async () => {
    await track(page, 'AF-100001');
    await expect(page.locator('#track-status')).toHaveText('In transit');
    await expect(page.locator('#track-destination')).toContainText('Frankfurt');
    expect(await page.locator('#track-events li').count(), 'a scan history').toBeGreaterThanOrEqual(3);
  });

  await test.step('track an unknown number', async () => {
    await track(page, 'AF-000000');
    await expect(page.locator('#track-not-found')).toBeVisible();
  });
});

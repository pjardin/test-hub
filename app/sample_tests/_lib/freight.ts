// Acme Freight helpers for the TypeScript tests -- the twin of _lib/freight.py.
//
// Paths are RELATIVE ('freight/...', never '/freight/...'): baseURL is the
// hub's target.url (the /demo/ root), so a relative path lands on the demo
// site even when the hub itself lives under a URL prefix. The password comes
// from Settings -> Credentials ("demo_password") when set, as
// PW_SECRET_DEMO_PASSWORD -- a real password never has to be written here.
import { expect, Page } from '@playwright/test';

export const DEMO_PASSWORD = process.env.PW_SECRET_DEMO_PASSWORD ?? 'demo-password';

/** Open a page of the demo site -- and say so plainly when the SITE is
 *  failing (an outage page also has a title and runs JavaScript, so without
 *  this a test would time out later on a missing element instead). */
export async function open(page: Page, path = ''): Promise<void> {
  const response = await page.goto(`freight/${path}`);
  if (response && response.status() >= 400) {
    throw new Error(`freight/${path} answered HTTP ${response.status()} (${await page.title()}) -- ` +
                    `the site itself is failing, not this test's steps`);
  }
}

export async function signIn(page: Page, user = 'tester'): Promise<void> {
  await open(page, 'login/');
  await page.fill('#username', user);
  await page.fill('#password', DEMO_PASSWORD);
  await page.click('#login-btn');
  await expect(page.locator('#signed-in-user')).toBeVisible();
}

export async function track(page: Page, shipmentId: string): Promise<void> {
  await open(page, 'track/');
  await page.fill('#track-id', shipmentId);
  await page.click('#track-btn');
  await expect(page.locator('#track-result, #track-not-found')).toBeVisible();
}

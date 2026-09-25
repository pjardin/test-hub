import { test, expect } from '@playwright/test';
import { randomBytes } from 'crypto';
import { open } from './_lib/freight';

// A password reset through the email, in TypeScript -- DEMO-023's flow on its
// own account (`courier`, so the two can run side by side). Waiting for the
// email is one web-first assertion with a longer timeout: the mailbox page
// reloads itself when new mail lands, and expect() keeps looking across the
// reloads. The reset link carries a secret token, so it is CLICKED in the
// email -- never navigated to, which would write the token into the log.
const ACCOUNT = 'courier';
const ADDRESS = 'courier@acme-freight.example';

test('a password reset works through the email -- exactly once', async ({ page }) => {
  await open(page, 'login/');
  await page.click('#forgot-link');
  await page.fill('#forgot-login', ACCOUNT);
  await page.click('#forgot-btn');
  const ref = (await page.locator('#forgot-ref').innerText()).trim();
  console.log(`reset requested, reference ${ref}`);

  const subject = page.locator(`#mail-list a.mail-subject:has-text('ref ${ref}')`);
  const resetLink = page.locator("#mail-body a[href*='/login/reset/']");
  await test.step('wait for the email', async () => {
    await open(page, `mailbox/?to=${ADDRESS}`);
    await expect(subject).toBeVisible({ timeout: 60_000 });
  });
  await subject.click();
  await expect(resetLink).toBeVisible();
  const message = page.url();

  const password = 'Pw-' + randomBytes(9).toString('base64url');   // fresh every run, never logged
  await test.step('choose a new password', async () => {
    await resetLink.click();
    await page.fill('#new-password', password);
    await page.fill('#confirm-password', password);
    await page.click('#reset-btn');
    await expect(page.locator('#login-btn')).toBeVisible();
  });

  await test.step('sign in with the new password', async () => {
    await page.fill('#username', ACCOUNT);
    await page.fill('#password', password);
    await page.click('#login-btn');
    await expect(page.locator('#signed-in-user')).toBeVisible();
  });
  await page.click('#sign-out');

  await test.step('follow the same link again', async () => {
    await page.goto(message);
    await resetLink.click();
    await expect(page.locator('#reset-invalid, #reset-form')).toBeVisible();
    await expect(page.locator('#reset-form'),
      'the reset link worked a SECOND time: any old reset email is still a key to the account')
      .toHaveCount(0);
  });
});

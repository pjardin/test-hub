import { test, expect } from '@playwright/test';

// Acme Freight's quote wizard, in TypeScript, through the PYTHON hub: the
// same queue, history, charts and schedules as the Python tests, executed by
// the TS engine's @playwright/test at /opt/pw-ts. baseURL is the hub's
// target.url (the /demo/ root), so a RELATIVE goto -- no leading slash --
// lands on the demo site even when the hub lives under a URL prefix.
test('a domestic quote prices the promo code correctly', async ({ page }) => {
  await page.goto('freight/quote/?restart=1');

  await test.step('route', async () => {
    await page.getByLabel('From (city)').fill('Oakland');
    await page.getByLabel('To (city)').fill('Los Angeles');
    await page.getByRole('button', { name: 'Next: cargo' }).click();
  });

  await test.step('cargo', async () => {
    await page.getByLabel('Total weight (kg)').fill('80');
    await page.getByRole('button', { name: 'Next', exact: true }).click();
  });

  await test.step('service', async () => {
    await page.getByRole('radio', { name: 'Express' }).check();
    await page.getByRole('button', { name: 'Next: review' }).click();
  });

  await test.step('promo code', async () => {
    await page.getByLabel('Promo code').fill('SPRING10');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(page.locator('#price-discount')).toBeVisible();
  });

  const amount = async (sel: string) => Number(await page.locator(sel).getAttribute('data-amount'));
  const subtotal = await amount('#price-subtotal');
  const discount = await amount('#price-discount');
  expect(discount, `SPRING10 should take 10% off $${subtotal}`).toBeCloseTo(subtotal * 0.10, 2);
  await expect(page.locator('#summary-service')).toHaveText('Express');
});

import { test, expect } from '@playwright/test';
import { open, signIn } from './_lib/freight';

// Drag-and-drop with rules, in TypeScript -- DEMO-021's checks with
// locator.dragTo(): allowed drops land, hazardous goods at a normal door are
// refused with the reason, and the board survives a reload (it is stored on
// the server, not just drawn).
test('trucks are scheduled by drag-and-drop, within the yard rules', async ({ page }) => {
  await signIn(page);
  await open(page, 'ops/docks/');

  const arrival = async (selector: string) =>
    (await page.locator(selector).first().getAttribute('data-arrival')) as string;
  const hazmat = await arrival("#arrivals .dock-card[data-hazardous='1']");
  const heavy = await arrival("#arrivals .dock-card[data-heavy='1'][data-hazardous='0']");
  const normal = await arrival("#arrivals .dock-card[data-heavy='0'][data-hazardous='0']");
  console.log(`hazmat ${hazmat}, heavy ${heavy}, normal ${normal}`);

  const card = (id: string) => page.locator(`.dock-card[data-arrival='${id}']`);
  const cell = (at: string) => page.locator(`td[data-cell='${at}']`);
  const placed = (id: string, at: string) => cell(at).locator(`.dock-card[data-arrival='${id}']`);

  await test.step('a normal load to Door 3, 09:00', async () => {
    await card(normal).dragTo(cell('D3|09:00'));
    await expect(placed(normal, 'D3|09:00')).toBeVisible();
  });

  await test.step('hazardous goods at Door 1 are refused', async () => {
    await card(hazmat).dragTo(cell('D1|10:00'));
    await expect(page.locator('#dock-error')).toBeVisible();
    await expect(page.locator('#dock-error')).toContainText(/hazardous/i);
    await expect(placed(hazmat, 'D1|10:00')).toHaveCount(0);
  });

  await test.step('hazardous goods at Door 4 (hazmat)', async () => {
    await card(hazmat).dragTo(cell('D4|10:00'));
    await expect(placed(hazmat, 'D4|10:00')).toBeVisible();
  });

  await test.step('a heavy load to a forklift door', async () => {
    await card(heavy).dragTo(cell('D1|11:00'));
    await expect(placed(heavy, 'D1|11:00')).toBeVisible();
  });

  await test.step('the board survives a reload', async () => {
    await page.reload();
    await expect(placed(normal, 'D3|09:00')).toBeVisible();
    await expect(placed(hazmat, 'D4|10:00')).toBeVisible();
    await expect(placed(heavy, 'D1|11:00')).toBeVisible();
  });
});

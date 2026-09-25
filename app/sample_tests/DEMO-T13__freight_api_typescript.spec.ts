import { test, expect } from '@playwright/test';

// The public API in TypeScript -- DEMO-020's promises, with two Playwright
// Test built-ins: the `request` fixture (HTTP with no page at all, relative
// to the same baseURL) and expect.soft (a broken promise is recorded and the
// test goes on, so one run reports every problem -- Python does that by hand).
const DEMO_KEY = process.env.PW_SECRET_FREIGHT_API_KEY ?? 'acme-demo-7f3a91c2';   // on the developers page
const TRIAL_KEY = 'acme-trial-2b91e4d0';                                          // 5 requests a minute
const API = 'freight/api/v1/';

test('the public API keeps its promises', async ({ request }) => {
  await test.step('auth: no key', async () => {
    const r = await request.get(API + 'shipments/AF-100001');
    expect.soft(r.status(), 'a request with no key must get 401').toBe(401);
  });

  await test.step('contract: a shipment', async () => {
    const r = await request.get(API + 'shipments/AF-100001', { headers: { 'X-API-Key': DEMO_KEY } });
    expect.soft(r.status()).toBe(200);
    if (r.ok()) {
      const body = await r.json();
      const has = Object.keys(body).sort().join(', ');
      for (const field of ['id', 'status', 'origin', 'destination', 'service', 'weight_kg', 'eta', 'events']) {
        expect.soft(body, `contract: field '${field}' is missing (the response has: ${has})`).toHaveProperty(field);
      }
    }
  });

  await test.step('money: SPRING10', async () => {
    const r = await request.post(API + 'quotes', {
      headers: { 'X-API-Key': DEMO_KEY },
      data: { origin: 'San Francisco', destination: 'San Diego', weight_kg: 120,
              service: 'standard', promo: 'SPRING10' },
    });
    expect.soft(r.status()).toBe(200);
    if (r.ok()) {
      const q = await r.json();
      expect.soft(Number(q.discount), `money: SPRING10 on a $${q.subtotal} subtotal is 10%`)
        .toBeCloseTo(Number(q.subtotal) * 0.10, 2);
    }
  });

  await test.step('limits: six calls on the trial key', async () => {
    const codes: number[] = [];
    let last = await request.get(API + 'shipments/AF-100002', { headers: { 'X-API-Key': TRIAL_KEY } });
    codes.push(last.status());
    for (let i = 1; i < 6; i++) {
      last = await request.get(API + 'shipments/AF-100002', { headers: { 'X-API-Key': TRIAL_KEY } });
      codes.push(last.status());
    }
    expect.soft(codes[codes.length - 1], `limits: 5 calls a minute, but 6 in a row answered ${codes}`).toBe(429);
    if (codes[codes.length - 1] === 429) {
      expect.soft(last.headers()['retry-after'], 'a 429 must say when to retry').toBeTruthy();
    }
  });
});

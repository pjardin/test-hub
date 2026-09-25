import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { open, signIn } from './_lib/freight';

// A shipping label in TypeScript -- DEMO-024's checks: "Print label" opens a
// NEW TAB (page.waitForEvent('popup') hands it over), the Code 128 barcode
// is read from its bars by the test's own decoder (below -- it shares no
// code with the site that printed it), and the PDF download is really a PDF
// carrying the tracking number.
const SHIPMENT = 'AF-100001';

// Code 128 bar/space widths of symbols 0..105 (the published table); stop = 2331112
const TABLE = ('212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 ' +
  '221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 221231 213212 ' +
  '223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 232121 111323 ' +
  '131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 113123 ' +
  '113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 ' +
  '312311 332111 314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 ' +
  '112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 111242 121142 ' +
  '121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 ' +
  '131141 114113 114311 411113 411311 113141 114131 311141 411131 211412 211214 211232').split(' ');

/** Code 128-B (the subset shipping labels use) from the bars' [x, width]. */
function decode(bars: [number, number][]): string {
  bars.sort((a, b) => a[0] - b[0]);
  const runs: number[] = [];
  bars.forEach(([x, w], i) => {
    if (i) runs.push(x - (bars[i - 1][0] + bars[i - 1][1]));   // the space before this bar
    runs.push(w);
  });
  const unit = Math.min(...runs);
  const modules = runs.map(r => Math.round(r / unit));
  if (modules.slice(-7).join('') !== '2331112') throw new Error('no stop symbol');
  const values: number[] = [];
  for (let i = 0; i + 7 < modules.length; i += 6) {
    const v = TABLE.indexOf(modules.slice(i, i + 6).join(''));
    if (v < 0) throw new Error(`symbol ${i / 6 + 1} is not Code 128`);
    values.push(v);
  }
  const [start, ...rest] = values;
  const check = rest.pop() as number;
  if (start !== 104) throw new Error(`start symbol ${start} is not Code 128-B`);
  const expected = (start + rest.reduce((sum, v, i) => sum + (i + 1) * v, 0)) % 103;
  if (check !== expected) throw new Error(`checksum ${check} != ${expected}: a misprint`);
  return rest.map(v => String.fromCharCode(v + 32)).join('');
}

test('the label opens in a new tab and its barcode reads right', async ({ page }) => {
  await signIn(page);
  await open(page, `ops/shipments/${SHIPMENT}/`);

  const label = await test.step('open the label (a new tab)', async () => {
    const [popup] = await Promise.all([page.waitForEvent('popup'), page.click('#print-label')]);
    await expect(popup.locator('#label-barcode')).toBeVisible();
    return popup;
  });

  await test.step('scan the barcode', async () => {
    const bars = await label.locator('#label-barcode g rect').evaluateAll(rects =>
      rects.map(r => [Number(r.getAttribute('x')), Number(r.getAttribute('width'))] as [number, number]));
    const scanned = decode(bars);
    console.log(`the barcode reads ${scanned}`);
    expect(scanned, `a depot scanner would route this parcel as ${scanned}`).toBe(SHIPMENT);
    await expect(label.locator('#label-number')).toHaveText(SHIPMENT);
  });

  await test.step('download the PDF', async () => {
    const [download] = await Promise.all([label.waitForEvent('download'), label.click('#label-pdf')]);
    const pdf = readFileSync((await download.path()) as string);
    expect(pdf.subarray(0, 5).toString(), 'the download is a PDF').toBe('%PDF-');
    expect(pdf.includes(`(${SHIPMENT}) Tj`), 'the PDF carries the tracking number').toBe(true);
  });
  await label.close();
});

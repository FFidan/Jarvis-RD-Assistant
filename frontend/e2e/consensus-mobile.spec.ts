import { expect, test } from '@playwright/test';
import { installMockedApiDefaults, seedAuthedSession } from './helpers/setup';

test('consensus bars remain readable on a 375-pixel viewport', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await seedAuthedSession(page);
  await installMockedApiDefaults(page);
  await page.route('**/api/consensus', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total: 1,
        truncated: false,
        claims: [
          {
            claim_topic: 'A long research claim that requires a compact mobile label',
            supports: 3,
            opposes: 1,
            paper_ids: [1, 2, 3, 4],
            assessments: [],
          },
        ],
      }),
    }),
  );

  await page.goto('/consensus');
  await expect(page.getByRole('heading', { name: 'Agreement by claim' })).toBeVisible();
  const bar = page.locator('.recharts-bar-rectangle path').first();
  await expect(bar).toBeVisible();
  expect((await bar.boundingBox())?.width ?? 0).toBeGreaterThan(10);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
});

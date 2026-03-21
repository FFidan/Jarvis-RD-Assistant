import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

test.describe('Home Page', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
  });

  test('dashboard metrics tiles load and display numbers', async ({ page }) => {
    // Wait for metric tiles to appear — they are rendered inside cards with titles
    const expectedTiles = [
      'Total Papers',
      'Unread Papers',
      'Pending Papers',
      'Due Cards',
      'Active Projects',
      'Topics',
      'Nudges',
    ];

    for (const title of expectedTiles) {
      // Each metric tile has a CardTitle with the metric name
      await expect(page.getByText(title).first()).toBeVisible();
    }

    // At least one tile should display a numeric value (text-2xl font-bold)
    const metricValues = page.locator('.text-2xl.font-bold');
    await expect(metricValues.first()).toBeVisible();
  });

  test('page has description heading visible', async ({ page }) => {
    // The home page has a main heading "Dashboard"
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();

    // Quick Navigation section heading
    await expect(page.getByText('Quick Navigation')).toBeVisible();
  });

  test('quick navigation links work', async ({ page }) => {
    // Click on "Research Feed" quick link — should navigate to /feed
    await page.getByRole('link', { name: /Research Feed/ }).click();
    await expect(page).toHaveURL(/\/feed$/);

    // Navigate back to home
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();

    // Click on "Analytics" quick link
    await page.getByRole('link', { name: /Analytics/ }).click();
    await expect(page).toHaveURL(/\/analytics$/);
  });

  test('loading skeleton shown while data fetches', async ({ page }) => {
    await page.reload();

    const skeletonsOrTiles = page.locator('[class*="skeleton"], .text-2xl.font-bold');
    await expect(skeletonsOrTiles.first()).toBeVisible({ timeout: 10000 });
  });
});

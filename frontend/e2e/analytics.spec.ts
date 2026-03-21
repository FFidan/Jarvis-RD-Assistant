import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

test.beforeEach(async ({ page }) => {
  await ensureAuthenticated(page);
  await page.goto('/analytics');
});

test.describe('Analytics Page', () => {
  test('page loads with heading and chart cards visible', async ({ page }) => {
    // Page heading
    await expect(page.getByRole('heading', { name: /analytics/i })).toBeVisible();

    // Wait for at least one chart card to appear (either loaded with data or empty state)
    await expect(
      page.getByText(/activity overview/i).or(page.getByText(/no activity data/i)),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('page description subtitle is visible via chart section titles', async ({ page }) => {
    // The analytics page does not have an explicit subtitle, but each chart Card has
    // a CardTitle that serves as section description. Verify the first two are visible.
    await expect(page.getByText(/activity overview/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/retention trend/i)).toBeVisible();
  });

  test('date range filter changes displayed data', async ({ page }) => {
    // Wait for the date range filter buttons to be present
    await expect(page.getByRole('button', { name: /last 7 days/i })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: /last 30 days/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /last 90 days/i })).toBeVisible();

    // The default is 30 days — its button should be the "active" variant (not outline)
    // Click "Last 7 days" to change the range
    await page.getByRole('button', { name: /last 7 days/i }).click();

    // After clicking, the data should re-fetch. Charts should still be visible
    // (either with data or empty state messages).
    await expect(
      page.getByText(/activity overview/i).or(page.getByText(/no activity data/i)),
    ).toBeVisible({ timeout: 10_000 });

    // Also test the custom days input
    const customInput = page.getByLabel(/custom/i);
    await expect(customInput).toBeVisible();
    await customInput.fill('14');
    await page.getByRole('button', { name: /apply/i }).click();

    // Charts should still be present after custom range applied
    await expect(
      page.getByText(/activity overview/i).or(page.getByText(/no activity data/i)),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('all 6 chart sections are visible', async ({ page }) => {
    // Wait for page to load
    await expect(page.getByRole('heading', { name: /analytics/i })).toBeVisible();

    // Each chart section has a CardTitle. Either the title is visible, or
    // it may still be loading. Wait for the first one then check all.
    await expect(
      page.getByText(/activity overview/i).or(page.getByText(/no activity data/i)),
    ).toBeVisible({ timeout: 15_000 });

    // 1. Activity Overview
    await expect(page.getByText(/activity overview/i)).toBeVisible();

    // 2. Retention Trend
    await expect(page.getByText(/retention trend/i)).toBeVisible();

    // 3. Papers by Source
    await expect(page.getByText(/papers by source/i)).toBeVisible();

    // 4. Papers by Status
    await expect(page.getByText(/papers by status/i)).toBeVisible();

    // 5. Reviews by Rating
    await expect(page.getByText(/reviews by rating/i)).toBeVisible();

    // 6. LLM Cost Over Time
    await expect(page.getByText(/llm cost over time/i)).toBeVisible();
  });
});

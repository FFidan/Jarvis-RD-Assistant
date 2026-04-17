import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

test.describe('My Day Page', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await page.goto('/my-day');
  });

  test('DayHeader renders time-of-day greeting and counter strip', async ({ page }) => {
    // DayHeader shows a greeting based on the current hour: one of
    // "Good morning" / "Good afternoon" / "Good evening".
    await expect(
      page.getByRole('heading', {
        name: /Good (morning|afternoon|evening)/,
      }),
    ).toBeVisible({ timeout: 10000 });

    // Counter strip labels (DayHeader)
    await expect(page.getByText('Pulse papers')).toBeVisible();
    await expect(page.getByText('Cards due')).toBeVisible();
    await expect(page.getByText('Tasks today')).toBeVisible();
    await expect(page.getByText('Unprocessed uploads')).toBeVisible();
  });

  test('PulsePreviewCard is present on My Day', async ({ page }) => {
    // PulsePreviewCard header contains "Today's Pulse" (with or without count suffix).
    await expect(page.getByText(/Today's Pulse/)).toBeVisible({ timeout: 10000 });
  });

  test('ActionItemsCard is present on My Day', async ({ page }) => {
    // ActionItemsCard title is "Action Items"
    await expect(page.getByRole('heading', { name: 'Action Items' })).toBeVisible({
      timeout: 10000,
    });
  });

  test('Focus + Tasks row renders', async ({ page }) => {
    // PomodoroTimer sibling card has "Today's Tasks" heading
    await expect(page.getByRole('heading', { name: "Today's Tasks" })).toBeVisible({
      timeout: 10000,
    });
  });

  test('counter strip displays numeric values after data loads', async ({ page }) => {
    // At least one tabular-nums value should appear once data loads
    const metricValues = page.locator('.tabular-nums');
    await expect(metricValues.first()).toBeVisible({ timeout: 10000 });
  });
});

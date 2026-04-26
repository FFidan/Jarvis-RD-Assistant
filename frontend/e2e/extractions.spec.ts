import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

test.beforeEach(async ({ page }) => {
  await seedAuthedSession(page);
  await page.goto('/extractions');
});

test.describe('Extraction Table Page', () => {
  test('template selector loads templates or shows empty state', async ({ page }) => {
    // Page heading
    await expect(page.getByRole('heading', { name: /extraction table/i })).toBeVisible();

    // Configuration card should be visible
    await expect(page.getByText(/configuration/i)).toBeVisible({ timeout: 10_000 });

    // Either templates loaded (selector visible) or empty state shows
    await expect(
      page
        .getByText(/extraction template/i)
        .or(page.getByText(/no templates/i))
        .or(page.getByText(/create an extraction template in settings/i)),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('extraction table shows data or empty state when template selected', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /extraction table/i })).toBeVisible();

    // Wait for the configuration section to load
    await expect(page.getByText(/configuration/i)).toBeVisible({ timeout: 10_000 });

    // Check the Comparison Table card
    await expect(page.getByText(/comparison table/i)).toBeVisible();

    // The table area should show one of:
    // - Actual data rows (if template + papers selected and data exists)
    // - "No extractions yet" (if papers selected but no data)
    // - "Select papers" (if no papers selected)
    // - "Choose a template and select papers" message
    await expect(
      page
        .getByText(/select papers/i)
        .or(page.getByText(/no extractions yet/i))
        .or(page.getByText(/choose a template and select papers/i))
        .or(page.locator('table')),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('extract button is disabled without template and papers', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /extraction table/i })).toBeVisible();

    // The "Extract Selected" button should exist
    const extractButton = page.getByRole('button', { name: /extract selected/i });
    await expect(extractButton).toBeVisible({ timeout: 10_000 });

    // Without papers selected, the button should be disabled
    await expect(extractButton).toBeDisabled();
  });
});

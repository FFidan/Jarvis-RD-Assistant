import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from '../helpers/auth';

test.describe('Settings - Sources', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await page.goto('/settings?section=sources&item=sources');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  });

  test('sources list loads', async ({ page }) => {
    // Wait for loading to complete
    await expect(page.getByText('Loading sources...')).not.toBeVisible({ timeout: 10000 });

    // Either sources are listed (with Enable/Disable toggles) or empty state
    const sourceCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const emptyState = page.getByText('No sources');

    const hasSources = await sourceCards.first().isVisible().catch(() => false);
    const hasEmptyState = await emptyState.isVisible().catch(() => false);
    expect(hasSources || hasEmptyState).toBeTruthy();
  });

  test('toggle source enabled/disabled', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading sources...')).not.toBeVisible({ timeout: 10000 });

    // This test only works if sources exist
    const sourceCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const hasSources = await sourceCards.first().isVisible().catch(() => false);

    if (!hasSources) {
      test.skip();
      return;
    }

    const firstSource = sourceCards.first();
    const isEnabled = await firstSource.getByText('Enabled').isVisible().catch(() => false);

    if (isEnabled) {
      await firstSource.getByRole('button', { name: 'Disable' }).click();
      await expect(firstSource.getByText('Disabled')).toBeVisible({ timeout: 10000 });
    } else {
      await firstSource.getByRole('button', { name: 'Enable' }).click();
      await expect(firstSource.getByText('Enabled')).toBeVisible({ timeout: 10000 });
    }
  });

  test('source config displays correctly', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading sources...')).not.toBeVisible({ timeout: 10000 });

    const sourceCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const hasSources = await sourceCards.first().isVisible().catch(() => false);

    if (!hasSources) {
      // Empty state should show descriptive text
      await expect(page.getByText('No paper sources configured')).toBeVisible();
      return;
    }

    // Each source card shows the source type (capitalized) and a Priority badge
    const firstSource = sourceCards.first();
    await expect(firstSource.getByText(/Priority:/)).toBeVisible();

    // The source type should be visible (e.g., "arxiv", "semantic_scholar")
    const sourceType = firstSource.locator('.font-medium.capitalize');
    await expect(sourceType).toBeVisible();
  });
});

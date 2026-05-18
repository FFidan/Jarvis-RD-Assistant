import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from '../helpers/auth';

test.describe('Settings - Authors', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await page.goto('/settings?section=research&item=authors');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  });

  test('authors list loads or shows empty state', async ({ page }) => {
    // Wait for loading to complete
    await expect(page.getByText('Loading authors...')).not.toBeVisible({ timeout: 10000 });

    // Either author cards are listed or empty state is shown
    const authorCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const emptyState = page.getByText('No tracked authors');

    const hasAuthors = await authorCards.first().isVisible().catch(() => false);
    const hasEmptyState = await emptyState.isVisible().catch(() => false);
    expect(hasAuthors || hasEmptyState).toBeTruthy();
  });

  test('create new tracked author', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading authors...')).not.toBeVisible({ timeout: 10000 });

    // Click "Add Author" button to open form
    await page.getByRole('button', { name: 'Add Author' }).click();

    // Fill in author form
    await page.getByLabel('Author Name').fill('Test Author E2E');
    await page.getByLabel(/Semantic Scholar ID/).fill('9999999');

    // Submit
    await page.getByRole('button', { name: 'Add Author' }).first().click();

    // Verify the new author appears
    await expect(page.getByText('Test Author E2E')).toBeVisible({ timeout: 10000 });
  });

  test('delete tracked author', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading authors...')).not.toBeVisible({ timeout: 10000 });

    // Create an author to delete if none exist
    const hasAuthors = await page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first()
      .isVisible()
      .catch(() => false);

    if (!hasAuthors) {
      await page.getByRole('button', { name: 'Add Author' }).click();
      await page.getByLabel('Author Name').fill('Author To Delete');
      await page.getByRole('button', { name: 'Add Author' }).first().click();
      await expect(page.getByText('Author To Delete')).toBeVisible({ timeout: 10000 });
    }

    const authorCountBefore = await page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .count();

    // Click the trash icon on the first author card
    const firstAuthorCard = page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first();
    await firstAuthorCard.getByRole('button').filter({ has: page.locator('svg') }).last().click();

    // Confirm deletion
    await expect(page.getByText('Delete Author')).toBeVisible();
    await page.getByRole('button', { name: 'Delete' }).click();

    // Wait for author count to decrease
    await expect(async () => {
      const currentCount = await page.locator('[class*="card"]')
        .filter({ hasText: /Enabled|Disabled/ })
        .count();
      const emptyVisible = await page.getByText('No tracked authors').isVisible().catch(() => false);
      expect(currentCount < authorCountBefore || emptyVisible).toBeTruthy();
    }).toPass({ timeout: 10000 });
  });

  test('auto-detect authors button triggers API call', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading authors...')).not.toBeVisible({ timeout: 10000 });

    // The "Auto-detect from starred/rated" button should be visible
    const autoDetectBtn = page.getByRole('button', { name: /Auto-detect/ });
    await expect(autoDetectBtn).toBeVisible();

    // Click it — the button text changes to "Detecting..." while pending
    await autoDetectBtn.click();

    // Either we see "Detecting..." briefly, or the result message appears
    const detectingOrResult = page.getByText(/Detecting|Added \d+ authors/);
    await expect(detectingOrResult.first()).toBeVisible({ timeout: 10000 });
  });

  test('check for papers button triggers API call', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading authors...')).not.toBeVisible({ timeout: 10000 });

    // The "Check now" button should be visible
    const checkBtn = page.getByRole('button', { name: /Check now/ });
    await expect(checkBtn).toBeVisible();

    // Click it — button text changes to "Checking..." while pending
    await checkBtn.click();

    // Either we see "Checking..." briefly, or the result message appears
    const checkingOrResult = page.getByText(/Checking|Checked \d+ authors/);
    await expect(checkingOrResult.first()).toBeVisible({ timeout: 10000 });
  });
});

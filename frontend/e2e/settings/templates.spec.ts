import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from '../helpers/auth';

test.describe('Settings - Extraction Templates', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await page.goto('/settings?section=system&item=extraction');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  });

  test('templates list loads or shows empty state', async ({ page }) => {
    // Wait for loading to complete
    await expect(page.getByText('Loading templates...')).not.toBeVisible({ timeout: 10000 });

    // Either template cards are listed or empty state
    const templateCards = page.locator('[class*="card"]').filter({ hasText: /fields/ });
    const emptyState = page.getByText('No extraction templates');

    const hasTemplates = await templateCards.first().isVisible().catch(() => false);
    const hasEmptyState = await emptyState.isVisible().catch(() => false);
    expect(hasTemplates || hasEmptyState).toBeTruthy();
  });

  test('create new extraction template', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading templates...')).not.toBeVisible({ timeout: 10000 });

    // Click "Add Template" button
    await page.getByRole('button', { name: 'Add Template' }).click();

    // Fill in the template form
    await page.getByLabel('Template Name').fill('Test Template E2E');
    await page.getByLabel(/Description/).fill('E2E test template');
    await page.getByLabel(/Fields/).fill(
      'method|Methodology|Research methodology used|text\nresult|Results|Key findings|text'
    );

    // Submit
    await page.getByRole('button', { name: 'Create Template' }).click();

    // Verify the new template appears
    await expect(page.getByText('Test Template E2E')).toBeVisible({ timeout: 10000 });
  });

  test('template displays field count and names', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading templates...')).not.toBeVisible({ timeout: 10000 });

    const templateCards = page.locator('[class*="card"]').filter({ hasText: /fields/ });
    const hasTemplates = await templateCards.first().isVisible().catch(() => false);

    if (!hasTemplates) {
      // Create one first
      await page.getByRole('button', { name: 'Add Template' }).click();
      await page.getByLabel('Template Name').fill('Display Test');
      await page.getByLabel(/Fields/).fill(
        'method|Methodology|Research methodology used|text\nresult|Results|Key findings|text'
      );
      await page.getByRole('button', { name: 'Create Template' }).click();
      await expect(page.getByText('Display Test')).toBeVisible({ timeout: 10000 });
    }

    // Each template card shows a "N fields" badge
    const firstTemplate = page.locator('[class*="card"]').filter({ hasText: /fields/ }).first();
    await expect(firstTemplate.getByText(/\d+ fields/)).toBeVisible();

    // Template card shows field names/labels
    const fieldLabels = firstTemplate.locator('.text-xs.text-muted-foreground');
    await expect(fieldLabels).toBeVisible();
  });

  test('delete template', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading templates...')).not.toBeVisible({ timeout: 10000 });

    // Create a template to delete if none exist
    const hasTemplates = await page.locator('[class*="card"]')
      .filter({ hasText: /fields/ })
      .first()
      .isVisible()
      .catch(() => false);

    if (!hasTemplates) {
      await page.getByRole('button', { name: 'Add Template' }).click();
      await page.getByLabel('Template Name').fill('Delete Me Template');
      await page.getByLabel(/Fields/).fill('test|Test|Test field|text');
      await page.getByRole('button', { name: 'Create Template' }).click();
      await expect(page.getByText('Delete Me Template')).toBeVisible({ timeout: 10000 });
    }

    const templateCountBefore = await page.locator('[class*="card"]')
      .filter({ hasText: /fields/ })
      .count();

    // Click the trash icon on the first template
    const firstTemplate = page.locator('[class*="card"]')
      .filter({ hasText: /fields/ })
      .first();
    await firstTemplate.getByRole('button').filter({ has: page.locator('svg') }).click();

    // Confirm deletion dialog
    await expect(page.getByText('Delete Template')).toBeVisible();
    await page.getByRole('button', { name: 'Delete' }).click();

    // Wait for template count to decrease or empty state
    await expect(async () => {
      const currentCount = await page.locator('[class*="card"]')
        .filter({ hasText: /fields/ })
        .count();
      const emptyVisible = await page.getByText('No extraction templates').isVisible().catch(() => false);
      expect(currentCount < templateCountBefore || emptyVisible).toBeTruthy();
    }).toPass({ timeout: 10000 });
  });
});

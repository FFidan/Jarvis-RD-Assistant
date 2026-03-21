import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from '../helpers/auth';

test.describe('Settings - Ingestion Config', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
    await page.getByRole('tab', { name: 'Ingestion' }).click();
  });

  test('config list loads', async ({ page }) => {
    // Wait for loading to complete
    await expect(page.getByText('Loading config...')).not.toBeVisible({ timeout: 10000 });

    // Either config entries are listed or empty state
    const configCards = page.locator('[class*="card"]').filter({ has: page.locator('.font-mono') });
    const emptyState = page.getByText('No config entries');

    const hasConfigs = await configCards.first().isVisible().catch(() => false);
    const hasEmptyState = await emptyState.isVisible().catch(() => false);
    expect(hasConfigs || hasEmptyState).toBeTruthy();
  });

  test('edit a config value via pencil icon', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading config...')).not.toBeVisible({ timeout: 10000 });

    const configCards = page.locator('[class*="card"]').filter({ has: page.locator('.font-mono') });
    const hasConfigs = await configCards.first().isVisible().catch(() => false);

    if (!hasConfigs) {
      test.skip();
      return;
    }

    // Click the pencil icon on the first config entry
    const firstConfig = configCards.first();
    await firstConfig.getByRole('button').filter({ has: page.locator('svg') }).click();

    // Edit mode should show an input field
    const editInput = firstConfig.locator('input');
    await expect(editInput).toBeVisible();

    // Type a new value
    await editInput.clear();
    await editInput.fill('test-value');

    // Click the check/save button (first button in edit mode)
    await firstConfig.getByRole('button').first().click();

    // Verify we exit edit mode (input should not be visible)
    await expect(firstConfig.locator('input')).not.toBeVisible({ timeout: 10000 });
  });

  test('config entries display key and value', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading config...')).not.toBeVisible({ timeout: 10000 });

    const configCards = page.locator('[class*="card"]').filter({ has: page.locator('.font-mono') });
    const hasConfigs = await configCards.first().isVisible().catch(() => false);

    if (!hasConfigs) {
      // Empty state should be informative
      await expect(page.getByText('Ingestion config will appear here once set')).toBeVisible();
      return;
    }

    // Each config card should display a monospaced key
    const firstConfig = configCards.first();
    const keyElement = firstConfig.locator('.font-mono');
    await expect(keyElement).toBeVisible();

    // And a value in muted-foreground
    const valueElement = firstConfig.locator('.text-muted-foreground');
    await expect(valueElement).toBeVisible();
  });

  test('config entries show structured keys', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading config...')).not.toBeVisible({ timeout: 10000 });

    const configCards = page.locator('[class*="card"]').filter({ has: page.locator('.font-mono') });
    const hasConfigs = await configCards.first().isVisible().catch(() => false);

    if (!hasConfigs) {
      test.skip();
      return;
    }

    // Config keys are displayed in monospace font — collect all keys
    const keys = await page.locator('.font-mono.text-sm').allTextContents();
    expect(keys.length).toBeGreaterThan(0);

    // Keys should be non-empty strings (e.g., "fsrs.desired_retention", "llm.model")
    for (const key of keys) {
      expect(key.trim().length).toBeGreaterThan(0);
    }
  });
});

import { test, expect } from '@playwright/test';
import { ensureAuthenticated, isAuthGateEnabled } from './helpers/auth';

test.describe('Authentication', () => {
  test('auth gate or bypass grants dashboard access', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(() => localStorage.removeItem('jarvis-auth'));
    await page.reload();

    await ensureAuthenticated(page);
  });

  test('invalid password stays on login page and shows error', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(() => localStorage.removeItem('jarvis-auth'));
    await page.reload();

    if (!(await isAuthGateEnabled(page))) {
      test.skip(true, 'Dashboard auth is bypassed in this environment');
    }

    await expect(page.getByLabel('Password')).toBeVisible();

    // Enter wrong password
    await page.getByLabel('Password').fill('wrong-password-123');
    await page.getByRole('button', { name: 'Sign In' }).click();

    // Should remain on login page with error message
    await expect(page.getByText('Invalid password')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'JARVIS RD Assistant' })).toBeVisible();

    // Password field should be cleared after failed attempt
    await expect(page.getByLabel('Password')).toHaveValue('');
  });

  test('session persists after page reload', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(() => localStorage.removeItem('jarvis-auth'));
    await page.reload();

    await ensureAuthenticated(page);

    // Reload page
    await page.reload();

    // Should still be on the authenticated dashboard (not login page)
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
    await expect(page.getByLabel('Password')).not.toBeVisible();
  });
});

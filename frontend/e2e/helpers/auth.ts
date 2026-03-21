import { expect, test, type Page } from '@playwright/test';

const TEST_PASSWORD = process.env.VITE_DASHBOARD_PASSWORD || 'test-password';

export async function isAuthGateEnabled(page: Page): Promise<boolean> {
  return page.getByLabel('Password').isVisible({ timeout: 1000 }).catch(() => false);
}

export async function ensureAuthenticated(page: Page): Promise<void> {
  await page.goto('/');

  if (await isAuthGateEnabled(page)) {
    await page.getByLabel('Password').fill(TEST_PASSWORD);
    await page.getByRole('button', { name: 'Sign In' }).click();
  }

  await expect(page.getByRole('button', { name: 'Logout' })).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('link', { name: 'Feed' })).toBeVisible();
}

export async function skipWhenAuthBypassed(page: Page, reason: string): Promise<void> {
  if (!(await isAuthGateEnabled(page))) {
    test.skip(true, reason);
  }
}

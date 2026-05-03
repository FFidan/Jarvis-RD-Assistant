import { expect, test, type Page } from '@playwright/test';

const TEST_API_KEY = process.env.JARVIS_API_KEY || 'test-key';
const AUTH_LABEL = /API Key|Password/i;

export async function isAuthGateEnabled(page: Page): Promise<boolean> {
  return page
    .getByLabel(AUTH_LABEL)
    .isVisible({ timeout: 1000 })
    .catch(() => false);
}

export async function ensureAuthenticated(page: Page): Promise<void> {
  await page.goto('/');

  if (await isAuthGateEnabled(page)) {
    await page.getByLabel(AUTH_LABEL).fill(TEST_API_KEY);
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

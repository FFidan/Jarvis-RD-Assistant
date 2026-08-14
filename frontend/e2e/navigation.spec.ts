import { test, expect, type Page } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

/**
 * The sidebar starts on the short "simple" rail, which carries only the daily
 * research loop. Open the grouped view so every destination is a direct link.
 */
async function showAllFeatures(page: Page) {
  const toggle = page.getByTestId('nav-mode-toggle');
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute('aria-label')) === 'Show all features') {
    await toggle.click();
  }
  await expect(toggle).toHaveAttribute('aria-label', 'Simple view');
}

test.describe('Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await showAllFeatures(page);
  });

  test('all sidebar links navigate to correct pages', async ({ page }) => {
    const sidebarLinks = [
      { label: 'Home', path: '/' },
      { label: 'My Day', path: '/my-day' },
      { label: 'Papers', path: '/feed\\?surface=library' },
      { label: 'Analytics', path: '/analytics' },
      { label: 'Projects', path: '/projects' },
      { label: 'Learning Cards', path: '/cards' },
      { label: 'Settings', path: '/settings' },
      { label: 'Citation Graph', path: '/citations' },
      { label: 'Knowledge Graph', path: '/knowledge' },
      { label: 'Extraction Table', path: '/extractions' },
    ];

    for (const link of sidebarLinks) {
      // Sidebar nav links contain both icon and text label
      const navLink = page.getByRole('link', { name: link.label });
      await navLink.click();
      await expect(page).toHaveURL(new RegExp(`${link.path}$`));
    }
  });

  test('health status dots are visible in sidebar', async ({ page }) => {
    // The sidebar shows two health status dots with labels
    await expect(page.getByText('Paper Ingestion')).toBeVisible();
    await expect(page.getByText('Learning Engine')).toBeVisible();

    // Health dots are small round spans: h-2 w-2 rounded-full
    const healthDots = page.locator('.rounded-full.h-2.w-2');
    await expect(healthDots).toHaveCount(2);
  });

  test('navigating to nonexistent path shows 404 page', async ({ page }) => {
    await page.goto('/nonexistent-page-12345');

    // The NotFoundPage renders "404" and "Page not found"
    await expect(page.getByText('404')).toBeVisible();
    await expect(page.getByText('Page not found')).toBeVisible();

    // It also has a "Go Home" link
    await expect(page.getByRole('link', { name: 'Go Home' })).toBeVisible();
  });

  test('browser back button works after navigation', async ({ page }) => {
    // Navigate: Home → Settings → Papers
    await page.getByRole('link', { name: 'Settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);

    await page.getByRole('link', { name: 'Papers' }).click();
    await expect(page).toHaveURL(/\/feed\?surface=library$/);

    // Go back to Settings
    await page.goBack();
    await expect(page).toHaveURL(/\/settings$/);

    // Go back to Home
    await page.goBack();
    await expect(page).toHaveURL(/\/$/);
  });
});

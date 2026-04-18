import { expect, test } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

test.describe('@live-smoke Live app smoke', () => {
  test('shell loads with sidebar, health labels, and dashboard heading', async ({ page }) => {
    await ensureAuthenticated(page);

    await expect(page.getByRole('link', { name: 'Research Feed' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'My Day' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Projects' })).toBeVisible();
    await expect(page.getByText('Paper Ingestion')).toBeVisible();
    await expect(page.getByText('Learning Engine')).toBeVisible();
  });

  test('feed, projects, and settings pages render stable live headings', async ({ page }) => {
    await ensureAuthenticated(page);

    await page.getByRole('link', { name: 'Research Feed' }).click();
    await expect(page).toHaveURL(/\/feed$/);
    await expect(page.getByRole('heading', { name: 'Research Feed' })).toBeVisible({
      timeout: 10000,
    });
    // Search tab uses a multi-source placeholder
    await page.getByRole('tab', { name: 'Search' }).click();
    await expect(page.getByPlaceholder(/Search your selected sources/)).toBeVisible();

    await page.getByRole('link', { name: 'Projects' }).click();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible({
      timeout: 10000,
    });
    await expect(page.getByText('Organize papers into research projects')).toBeVisible();

    await page.getByRole('link', { name: 'Settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.getByRole('heading', { name: 'Settings' }).first()).toBeVisible({
      timeout: 10000,
    });
    await expect(page.getByRole('tab', { name: 'Topics' })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'Sources' })).toBeVisible();
  });

  test('citation graph live page renders a safe empty-or-loaded state', async ({ page }) => {
    await ensureAuthenticated(page);

    await page.getByRole('link', { name: 'Citation Graph' }).click();
    await expect(page).toHaveURL(/\/citations$/);
    await expect(page.getByRole('heading', { name: 'Citation Graph' })).toBeVisible({
      timeout: 10000,
    });

    await expect(
      page
        .getByText('No citations loaded')
        .or(page.getByPlaceholder('Search papers to add to citation graph...'))
        .or(page.locator('canvas'))
        .or(page.getByText('No citation data')),
    ).toBeVisible({ timeout: 10000 });
  });
});

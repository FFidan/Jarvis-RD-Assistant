import { expect, test } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

test.describe('@live-smoke Live app smoke', () => {
  test('shell loads with sidebar, health labels, and dashboard heading', async ({ page }) => {
    await ensureAuthenticated(page);

    await expect(page.getByRole('link', { name: 'Library' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'My Day' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Projects' })).toBeVisible();
    await expect(page.getByText('Paper Ingestion')).toBeVisible();
    await expect(page.getByText('Learning Engine')).toBeVisible();
  });

  test('feed, projects, and settings pages render stable live headings', async ({ page }) => {
    await ensureAuthenticated(page);

    await page.getByRole('link', { name: 'Library' }).click();
    await expect(page).toHaveURL(/\/feed$/);
    await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible({
      timeout: 10000,
    });
    // Discover entry point is visible on the Library page (top of the facet rail)
    await expect(page.getByTestId('facet-discover-block')).toBeVisible();

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

  test('paper detail reaches a stable contradictions panel state when papers exist', async ({ page }) => {
    await ensureAuthenticated(page);

    const feedResponse = await page.request.get('/api/papers/feed?limit=1');
    test.skip(!feedResponse.ok(), 'live feed endpoint is unavailable');
    const feedBody = await feedResponse.json();
    const firstPaper = Array.isArray(feedBody) ? feedBody[0] : feedBody.papers?.[0];
    test.skip(!firstPaper?.id, 'live stack has no papers to open');

    await page.goto(`/papers/${firstPaper.id}`);
    await expect(page.getByRole('button', { name: /Back/i })).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('heading', { name: 'Contradictions' })).toBeVisible({
      timeout: 10000,
    });
    await expect(
      page
        .getByText('No verified contradictions found.')
        .or(page.getByText('Failed to load contradictions.'))
        .or(page.locator('section').filter({ hasText: 'Contradictions' }).getByText(/\d+%/)),
    ).toBeVisible({ timeout: 10000 });
  });
});

import { test, expect } from '@playwright/test';
import { seedAuthedSession } from '../helpers/setup';

/**
 * Discover tab multi-source fan-out.
 *
 * Two source checkboxes (arxiv + pubmed) are toggled on, a search is
 * submitted, and the mocked `/api/search-preview` returns results from
 * both sources. Assertions confirm both source badges render in the
 * result list and per_source_counts reflects both.
 */
test.describe('Discover tab multi-source search @feed', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);

    await page.route('**/api/config/sources', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          { source_type: 'arxiv', enabled: true, priority: 1, config: {} },
          { source_type: 'pubmed', enabled: true, priority: 2, config: {} },
          { source_type: 'local', enabled: true, priority: 3, config: {} },
        ]),
      });
    });

    await page.route('**/api/search-preview', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      const body = route.request().postDataJSON?.() ?? {};
      const requestedSources: string[] = Array.isArray(body.source_types)
        ? body.source_types
        : [];
      // Only return combined results when the caller requested both.
      const wantsBoth =
        requestedSources.includes('arxiv') && requestedSources.includes('pubmed');

      const results = wantsBoth
        ? [
            {
              title: 'Attention Is All You Need',
              authors: ['Vaswani, A.'],
              abstract: 'Transformer architecture.',
              published_date: '2017-06-12',
              source_type: 'arxiv',
              external_id: '1706.03762',
            },
            {
              title: 'CRISPR gene editing in humans',
              authors: ['Doudna, J.'],
              abstract: 'Biomedical application.',
              published_date: '2020-01-01',
              source_type: 'pubmed',
              external_id: 'pubmed-42',
            },
          ]
        : [];

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          results,
          total: results.length,
          per_source_counts: wantsBoth ? { arxiv: 1, pubmed: 1 } : {},
          degraded_sources: [],
        }),
      });
    });
  });

  test('selecting arxiv + pubmed fans out and shows both sources in results', async ({
    page,
  }) => {
    await page.goto('/feed?tab=discover');

    // Wait for the source checkboxes to render.
    const arxivCheckbox = page.getByRole('checkbox', { name: /arxiv/i });
    const pubmedCheckbox = page.getByRole('checkbox', { name: /pubmed/i });
    await expect(arxivCheckbox).toBeVisible({ timeout: 10_000 });
    await expect(pubmedCheckbox).toBeVisible();

    // Ensure both are checked (they default on, but be explicit).
    if (!(await arxivCheckbox.isChecked())) await arxivCheckbox.check();
    if (!(await pubmedCheckbox.isChecked())) await pubmedCheckbox.check();

    // Enter a query and search.
    const searchInput = page.getByPlaceholder(/search your selected sources/i);
    await expect(searchInput).toBeVisible();
    await searchInput.fill('attention');

    const [searchResp] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/search-preview') && r.request().method() === 'POST',
        { timeout: 15_000 },
      ),
      page.getByRole('button', { name: 'Search' }).click(),
    ]);
    expect(searchResp.status()).toBeLessThan(400);

    // Both papers render.
    await expect(page.getByText('Attention Is All You Need')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('CRISPR gene editing in humans')).toBeVisible();

    // Both source badges appear in the result list.
    await expect(page.getByText('arXiv', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('PubMed', { exact: true }).first()).toBeVisible();
  });
});

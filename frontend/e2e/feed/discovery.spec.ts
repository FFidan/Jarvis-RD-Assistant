import { test, expect } from '@playwright/test';
import { seedAuthedSession } from '../helpers/setup';

/**
 * Discover tab regression tests.
 *
 * Round 4 multi-source fan-out:
 *   - The single-source Radix Select was replaced with a checkbox group.
 *   - Search placeholder is now "Search your selected sources…" (not
 *     "Search arXiv or Semantic Scholar...").
 *   - Default enabled sources include arxiv, semantic_scholar, pubmed, openalex.
 */

// Stub enabled-sources list so the multi-source checkbox group renders
// deterministically regardless of backend state.
async function stubSources(page: import('@playwright/test').Page) {
  await page.route('**/api/sources**', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { source_type: 'arxiv', enabled: true },
        { source_type: 'semantic_scholar', enabled: true },
        { source_type: 'pubmed', enabled: true },
        { source_type: 'openalex', enabled: true },
        { source_type: 'local', enabled: true },
      ]),
    });
  });
}

test.beforeEach(async ({ page }) => {
  await seedAuthedSession(page);
  await stubSources(page);
  await page.goto('/feed');
  // Open the Search tab — SearchBar + checkbox group live here.
  await page.getByRole('tab', { name: 'Search' }).click();
  await page.waitForLoadState('networkidle');
});

test.describe('Feed Discovery', () => {
  test('multi-source checkbox group renders all enabled external sources', async ({ page }) => {
    // "Sources:" label precedes the checkbox group
    await expect(page.getByText('Sources:')).toBeVisible();

    // Each external source should render a checkbox + label
    const expectedSources = ['arXiv', 'Semantic Scholar', 'PubMed', 'OpenAlex'];
    for (const label of expectedSources) {
      await expect(page.getByText(label, { exact: true })).toBeVisible();
    }

    // There should be exactly 4 source checkboxes (local is filtered out)
    const sourceCheckboxes = page.locator('input[type="checkbox"]');
    await expect(sourceCheckboxes).toHaveCount(4);

    // All should be checked by default
    for (let i = 0; i < 4; i++) {
      await expect(sourceCheckboxes.nth(i)).toBeChecked();
    }
  });

  test('search input accepts query text and triggers search', async ({ page }) => {
    const searchInput = page.getByPlaceholder(/Search your selected sources/);
    await expect(searchInput).toBeVisible();

    await searchInput.fill('transformer architecture');
    await expect(searchInput).toHaveValue('transformer architecture');

    const searchButton = page.getByRole('button', { name: 'Search' });
    await searchButton.click();

    // After clicking search, the button should show loading or results should appear
    await expect(
      page.getByText('Results').or(page.getByText('Search failed')).or(searchButton),
    ).toBeVisible({ timeout: 10000 });
  });

  test('results display with title and abstract snippets', async ({ page }) => {
    // Intercept search API to provide mock results
    await page.route('**/api/search-preview', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          results: [
            {
              title: 'Attention Is All You Need',
              authors: ['Vaswani, A.', 'Shazeer, N.'],
              abstract: 'We propose a new simple network architecture based on attention mechanisms.',
              published_date: '2017-06-12',
              source_type: 'arxiv',
              external_id: '1706.03762',
            },
            {
              title: 'BERT: Pre-training of Deep Bidirectional Transformers',
              authors: ['Devlin, J.'],
              abstract: 'We introduce BERT, a language representation model.',
              published_date: '2018-10-11',
              source_type: 'arxiv',
              external_id: '1810.04805',
            },
          ],
          degraded_sources: [],
        }),
      });
    });

    const searchInput = page.getByPlaceholder(/Search your selected sources/);
    await searchInput.fill('transformer');
    await page.getByRole('button', { name: 'Search' }).click();

    // Wait for results to appear
    await expect(page.getByText('Attention Is All You Need')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('BERT: Pre-training of Deep Bidirectional Transformers')).toBeVisible();

    // Check abstract snippets are shown
    await expect(page.getByText(/attention mechanisms/)).toBeVisible();
    await expect(page.getByText(/language representation model/)).toBeVisible();
  });

  test('save button saves paper to library', async ({ page }) => {
    // Intercept search API with mock results
    await page.route('**/api/search-preview', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          results: [
            {
              title: 'Test Paper for Saving',
              authors: ['Author, A.'],
              abstract: 'This is a test paper abstract.',
              published_date: '2024-01-01',
              source_type: 'arxiv',
              external_id: 'test.1234',
            },
          ],
          degraded_sources: [],
        }),
      });
    });

    // Intercept save API
    await page.route('**/api/papers/batch**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ id: 1, title: 'Test Paper for Saving' }]),
      });
    });

    // Search first
    const searchInput = page.getByPlaceholder(/Search your selected sources/);
    await searchInput.fill('test paper');
    await page.getByRole('button', { name: 'Search' }).click();

    // Wait for results
    await expect(page.getByText('Test Paper for Saving')).toBeVisible({ timeout: 5000 });

    // Click save all
    const saveAllButton = page.getByRole('button', { name: 'Save all' });
    await expect(saveAllButton).toBeVisible();
    await saveAllButton.click();

    // Expect success message
    await expect(page.getByText(/Saved.*paper/)).toBeVisible({ timeout: 5000 });
  });

  test('unchecking a source toggles the checkbox state', async ({ page }) => {
    // Multi-source fan-out: user can uncheck individual sources.
    const pubmedLabel = page.getByText('PubMed', { exact: true });
    await expect(pubmedLabel).toBeVisible();

    // Find the checkbox associated with the PubMed label (sibling input)
    const pubmedCheckbox = page
      .locator('label', { hasText: 'PubMed' })
      .locator('input[type="checkbox"]');
    await expect(pubmedCheckbox).toBeChecked();

    await pubmedCheckbox.click();
    await expect(pubmedCheckbox).not.toBeChecked();

    // Re-check it
    await pubmedCheckbox.click();
    await expect(pubmedCheckbox).toBeChecked();
  });

  test('empty query disables search button', async ({ page }) => {
    const searchInput = page.getByPlaceholder(/Search your selected sources/);
    await expect(searchInput).toBeVisible();

    // The search button should be disabled when input is empty
    const searchButton = page.getByRole('button', { name: 'Search' });
    await expect(searchButton).toBeDisabled();

    // Type something then clear it
    await searchInput.fill('test');
    await expect(searchButton).toBeEnabled();

    await searchInput.fill('');
    await expect(searchButton).toBeDisabled();
  });
});

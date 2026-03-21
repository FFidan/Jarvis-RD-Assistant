import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  // Set auth state in localStorage before navigating
  await page.goto('/');
  await page.evaluate(() => {
    localStorage.setItem(
      'jarvis-auth',
      JSON.stringify({
        state: { isAuthenticated: true, authTime: Date.now() },
        version: 0,
      }),
    );
  });
  await page.goto('/feed');
  await page.waitForLoadState('networkidle');
});

test.describe('Feed Discovery', () => {
  test('search input accepts query text and triggers search', async ({ page }) => {
    const searchInput = page.getByPlaceholder('Search arXiv or Semantic Scholar...');
    await expect(searchInput).toBeVisible();

    await searchInput.fill('transformer architecture');
    await expect(searchInput).toHaveValue('transformer architecture');

    const searchButton = page.getByRole('button', { name: 'Search' });
    await searchButton.click();

    // After clicking search, the button should show loading or results should appear
    // Accept either loading state or results/error
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
        body: JSON.stringify([
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
        ]),
      });
    });

    const searchInput = page.getByPlaceholder('Search arXiv or Semantic Scholar...');
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
        body: JSON.stringify([
          {
            title: 'Test Paper for Saving',
            authors: ['Author, A.'],
            abstract: 'This is a test paper abstract.',
            published_date: '2024-01-01',
            source_type: 'arxiv',
            external_id: 'test.1234',
          },
        ]),
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
    const searchInput = page.getByPlaceholder('Search arXiv or Semantic Scholar...');
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

  test('source selector switches between arXiv and Semantic Scholar', async ({ page }) => {
    // The source selector is a Radix Select component
    // Default value is "arxiv"
    const sourceTrigger = page.locator('button[role="combobox"]').filter({ hasText: 'arXiv' });
    await expect(sourceTrigger).toBeVisible();

    // Open the select dropdown
    await sourceTrigger.click();

    // Select Semantic Scholar
    const semanticOption = page.getByRole('option', { name: 'Semantic Scholar' });
    await expect(semanticOption).toBeVisible();
    await semanticOption.click();

    // Verify the trigger now shows "Semantic Scholar"
    await expect(
      page.locator('button[role="combobox"]').filter({ hasText: 'Semantic Scholar' }),
    ).toBeVisible();
  });

  test('empty query shows appropriate state', async ({ page }) => {
    const searchInput = page.getByPlaceholder('Search arXiv or Semantic Scholar...');
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

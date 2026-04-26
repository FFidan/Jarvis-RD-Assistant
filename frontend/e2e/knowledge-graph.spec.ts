import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

test.beforeEach(async ({ page }) => {
  await seedAuthedSession(page);
});

test.describe('Knowledge Graph Page', () => {
  test('page loads showing either empty state message or graph canvas', async ({ page }) => {
    await page.goto('/knowledge');
    await page.waitForLoadState('networkidle');

    // Page title should be visible
    const main = page.locator('main');
    await expect(main.getByRole('heading', { name: 'Knowledge Graph' })).toBeVisible({
      timeout: 5000,
    });

    // Either the empty state or the Cytoscape canvas should be present
    const emptyState = page.getByText('No entities extracted yet');
    const graphCanvas = page.locator('canvas');
    const loadingError = page.getByText(/Failed to load knowledge graph/);

    await expect(
      emptyState.or(graphCanvas).or(loadingError),
    ).toBeVisible({ timeout: 10000 });
  });

  test('entity type filter dropdown is present and functional', async ({ page }) => {
    // Mock knowledge graph API with some data
    await page.route('**/api/knowledge-graph?**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          entities: [
            {
              id: 1,
              name: 'BERT',
              entity_type: 'method',
              description: 'Pre-trained language model',
              paper_count: 5,
              canonical_name: 'BERT',
            },
            {
              id: 2,
              name: 'ImageNet',
              entity_type: 'dataset',
              description: 'Large-scale image dataset',
              paper_count: 10,
              canonical_name: 'ImageNet',
            },
          ],
          relationships: [],
        }),
      });
    });

    await page.goto('/knowledge');
    await page.waitForLoadState('networkidle');

    // The entity type filter should be visible
    const entityTypeLabel = page.getByText('Entity Type', { exact: true });
    await expect(entityTypeLabel).toBeVisible({ timeout: 5000 });

    // Open the entity type dropdown
    // The EntityTypeFilter uses a Select with default "All"
    const entityTypeTrigger = page
      .locator('button[role="combobox"]')
      .filter({ hasText: 'All' });
    await expect(entityTypeTrigger).toBeVisible();
    await entityTypeTrigger.click();

    // Verify filter options are available
    await expect(page.getByRole('option', { name: 'Method' })).toBeVisible();
    await expect(page.getByRole('option', { name: 'Dataset' })).toBeVisible();
    await expect(page.getByRole('option', { name: 'Concept' })).toBeVisible();

    // Select "Method"
    await page.getByRole('option', { name: 'Method' }).click();

    // Verify the dropdown now shows "Method"
    await expect(
      page.locator('button[role="combobox"]').filter({ hasText: 'Method' }),
    ).toBeVisible();
  });

  test('min paper count slider adjusts filter', async ({ page }) => {
    await page.route('**/api/knowledge-graph?**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ entities: [], relationships: [] }),
      });
    });

    await page.goto('/knowledge');
    await page.waitForLoadState('networkidle');

    // The min paper count slider should be visible
    const sliderLabel = page.getByText(/Min Paper Count/);
    await expect(sliderLabel).toBeVisible({ timeout: 5000 });

    // The range input should be present
    const slider = page.locator('input[type="range"]').first();
    await expect(slider).toBeVisible();

    // Verify default value is 1
    await expect(slider).toHaveValue('1');

    // Change the slider value
    await slider.fill('5');
    await expect(slider).toHaveValue('5');

    // The label should update to show the new value
    await expect(page.getByText('Min Paper Count: 5')).toBeVisible();
  });

  test('query input triggers knowledge graph search', async ({ page }) => {
    // Mock the knowledge graph query API
    await page.route('**/api/knowledge-graph/query**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          results: [
            { entity: 'ResNet', type: 'method', relevance: 0.95 },
          ],
        }),
      });
    });

    // Mock the main knowledge graph API
    await page.route('**/api/knowledge-graph?**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ entities: [], relationships: [] }),
      });
    });

    await page.goto('/knowledge');
    await page.waitForLoadState('networkidle');

    // Find the query input
    const queryInput = page.getByPlaceholder("Query (e.g., 'What methods are used on ImageNet?')");
    await expect(queryInput).toBeVisible({ timeout: 5000 });

    // Type a query
    await queryInput.fill('What methods are used on ImageNet?');

    // Click the Query button
    const queryButton = page.getByRole('button', { name: 'Query' });
    await expect(queryButton).toBeEnabled();
    await queryButton.click();

    // Wait for results or error
    const queryResults = page.getByText('Query Results');
    const noResults = page.getByText('No results found');
    const queryError = page.getByText(/Query failed/);

    await expect(
      queryResults.or(noResults).or(queryError),
    ).toBeVisible({ timeout: 10000 });
  });
});

import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
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
});

test.describe('Citation Graph Page', () => {
  test('page loads showing either empty state message or graph canvas', async ({ page }) => {
    await page.goto('/citations');
    await page.waitForLoadState('networkidle');

    // Page title
    await expect(page.getByRole('heading', { name: 'Citation Graph' })).toBeVisible({
      timeout: 5000,
    });

    const emptyState = page.getByText('No citations loaded');
    const graphCanvas = page.locator('canvas');
    const noCitationData = page.getByText('No citation data');
    const paperSearch = page.getByPlaceholder('Search papers to add to citation graph...');

    await expect(
      emptyState.or(graphCanvas).or(noCitationData).or(paperSearch),
    ).toBeVisible({ timeout: 10000 });
  });

  test('paper selector dropdown lists papers', async ({ page }) => {
    // Mock the papers brief API
    await page.route('**/api/papers/brief**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          { id: 1, title: 'Attention Is All You Need' },
          { id: 2, title: 'BERT: Pre-training of Deep Bidirectional Transformers' },
          { id: 3, title: 'GPT-3: Language Models are Few-Shot Learners' },
        ]),
      });
    });

    await page.goto('/citations');
    await page.waitForLoadState('networkidle');

    // The paper selector search input should be visible
    const paperSearchInput = page.getByPlaceholder('Search papers to add to citation graph...');
    await expect(paperSearchInput).toBeVisible({ timeout: 5000 });

    // Type to search for papers
    await paperSearchInput.fill('Attention');

    // Wait for the dropdown suggestions
    await expect(page.getByText('Attention Is All You Need')).toBeVisible({ timeout: 5000 });

    // Click to select a paper
    await page.getByText('Attention Is All You Need').click();

    // The selected paper should appear as a badge
    await expect(
      page.locator('[class*="badge"]', { hasText: 'Attention Is All You Need' }).or(
        page.getByText('1/10 papers selected'),
      ),
    ).toBeVisible();

    // Selection counter should update
    await expect(page.getByText(/1\/10 papers selected/)).toBeVisible();
  });

  test('layout toggle switches between graph layouts', async ({ page }) => {
    await page.goto('/citations');
    await page.waitForLoadState('networkidle');

    // The layout selector should be visible (GraphControls component)
    const layoutLabel = page.getByText('Layout:');
    await expect(layoutLabel).toBeVisible({ timeout: 5000 });

    // Open the layout dropdown -- default is "Force-directed (CoSE)"
    const layoutTrigger = page
      .locator('button[role="combobox"]')
      .filter({ hasText: 'Force-directed' });
    await expect(layoutTrigger).toBeVisible();
    await layoutTrigger.click();

    // Verify layout options are available
    await expect(page.getByRole('option', { name: 'Breadth-first' })).toBeVisible();
    await expect(page.getByRole('option', { name: 'Circle' })).toBeVisible();
    await expect(page.getByRole('option', { name: 'Concentric' })).toBeVisible();

    // Select a different layout
    await page.getByRole('option', { name: 'Circle' }).click();

    // Verify the layout trigger now shows "Circle"
    await expect(
      page.locator('button[role="combobox"]').filter({ hasText: 'Circle' }),
    ).toBeVisible();

    // Switch to another layout
    await page.locator('button[role="combobox"]').filter({ hasText: 'Circle' }).click();
    await page.getByRole('option', { name: 'Breadth-first' }).click();

    await expect(
      page.locator('button[role="combobox"]').filter({ hasText: 'Breadth-first' }),
    ).toBeVisible();
  });
});

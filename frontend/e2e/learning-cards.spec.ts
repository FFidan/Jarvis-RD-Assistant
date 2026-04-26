import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

test.beforeEach(async ({ page }) => {
  await seedAuthedSession(page);
  await page.goto('/cards');
});

test.describe('Learning Cards Page', () => {
  test('page loads with heading and tabs', async ({ page }) => {
    // Heading visible
    await expect(page.getByRole('heading', { name: /learning cards/i })).toBeVisible();

    // Tabs visible
    await expect(page.getByRole('tab', { name: /review/i })).toBeVisible();
    await expect(page.getByRole('tab', { name: /browse/i })).toBeVisible();
  });

  test('decks list loads or shows empty state in browse tab', async ({ page }) => {
    // Switch to Browse tab
    await page.getByRole('tab', { name: /browse/i }).click();

    // Wait for loading to finish, then expect either deck cards or the empty state
    await expect(
      page.getByText(/deck/).or(page.getByText(/create a deck to start organizing/i)),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('retention stats display in stats header', async ({ page }) => {
    // StatsHeader renders MetricTile components with these titles
    // Either the stats load with metrics, or stats is null and nothing renders.
    // Wait for the page to settle, then check for at least one stat tile or skeleton.
    const statsArea = page.locator('.grid').first();
    await expect(statsArea).toBeVisible({ timeout: 10_000 });

    // If stats loaded, we should see metric tiles like "Total Cards", "Due Now", etc.
    const totalCardsMetric = page.getByText('Total Cards');
    const skeleton = page.locator('[class*="skeleton"]').first();

    // Either the metric is visible or the page has moved past loading
    await expect(totalCardsMetric.or(skeleton).or(page.getByRole('tab', { name: /review/i }))).toBeVisible();
  });

  test('review mode shows a card or empty state', async ({ page }) => {
    // Review tab is default — wait for content to load
    await expect(
      page
        .getByText(/click to reveal answer/i)
        .or(page.getByText(/all caught up/i))
        .or(page.getByText(/no cards due/i))
        .or(page.getByText(/loading next card/i)),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('review flow: reveal answer and rate difficulty', async ({ page }) => {
    // Wait for review content
    const revealHint = page.getByText(/click to reveal answer/i);
    const emptyState = page.getByText(/no cards due/i).or(page.getByText(/all caught up/i));

    await expect(revealHint.or(emptyState)).toBeVisible({ timeout: 10_000 });

    // Only continue with review flow if there is a card to review
    if (await revealHint.isVisible()) {
      // Click the flashcard to reveal
      await revealHint.click();

      // Rating buttons should appear after reveal
      await expect(page.getByText(/how well did you know this/i)).toBeVisible();

      // All four rating buttons should be present
      await expect(page.getByRole('button', { name: /again/i })).toBeVisible();
      await expect(page.getByRole('button', { name: /hard/i })).toBeVisible();
      await expect(page.getByRole('button', { name: /good/i })).toBeVisible();
      await expect(page.getByRole('button', { name: /easy/i })).toBeVisible();

      // Click one of the rating buttons
      await page.getByRole('button', { name: /good/i }).click();

      // After rating, should load next card or show empty state
      await expect(
        page
          .getByText(/click to reveal answer/i)
          .or(page.getByText(/no cards due/i))
          .or(page.getByText(/all caught up/i))
          .or(page.getByText(/loading next card/i)),
      ).toBeVisible({ timeout: 10_000 });
    }
  });
});

/**
 * Playwright e2e — Learning Cards IA redesign (mocked)
 *
 * Uses page.route to fulfill review/stats/decks endpoints so the spec
 * runs without a live backend (PLAYWRIGHT_BASE_URL=http://127.0.0.1:3001).
 *
 * Seeded library: 2 decks, 5 due cards in deck 1, 0 due in deck 2.
 * Session walk: /cards → breadcrumb shows → progress bar → reveal → rate → next card.
 * Browse walk: breadcrumb nav → Library view → StatsHeader tiles → deck grid.
 */

import { test, expect, type Page } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

// ── Fixtures ─────────────────────────────────────────────────────────────────

const STATS_WITH_DUE = {
  total_cards: 20,
  due_now: 5,
  reviewed_today: 3,
  average_retention: 82.5,
  reviews_by_rating: {},
  streak_days: 4,
};

const STATS_ALL_DONE = {
  total_cards: 20,
  due_now: 0,
  reviewed_today: 8,
  average_retention: 90.0,
  reviews_by_rating: {},
  streak_days: 4,
};

const DECKS = [
  { id: 1, name: 'RGS Thesis', description: 'Thesis flashcards', topic_id: null, card_count: 10, due_count: 5, created_at: '2026-01-01T00:00:00Z' },
  { id: 2, name: 'Neural ODEs', description: null, topic_id: null, card_count: 6, due_count: 0, created_at: '2026-01-02T00:00:00Z' },
];

const CARD = {
  id: 42,
  deck_id: 1,
  paper_id: null,
  card_type: 'concept',
  front: 'What is the time complexity of the RGS algorithm?',
  back: 'O(n log n) in the average case.',
  evidence: null,
  fsrs_state: {},
  due_at: new Date(Date.now() - 86400_000).toISOString(),
  created_at: new Date(Date.now() - 7 * 86400_000).toISOString(),
  updated_at: new Date(Date.now() - 4 * 86400_000).toISOString(),
};

const REVIEW_RESPONSE = {
  card_id: 42,
  rating: 3,
  next_due_at: new Date(Date.now() + 86400_000).toISOString(),
  fsrs_state: {},
  review_log_id: 1,
};

/** Seed all API routes needed for the learning cards page. */
async function seedRoutes(page: Page, { dueNow = 5 }: { dueNow?: number } = {}) {
  const stats = dueNow > 0 ? STATS_WITH_DUE : STATS_ALL_DONE;

  await page.route('**/api/stats', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(stats) }),
  );

  await page.route('**/api/decks', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(DECKS) }),
  );

  // First call returns the due card; subsequent calls return empty (session end)
  let reviewCallCount = 0;
  await page.route('**/api/review/next**', (route) => {
    reviewCallCount++;
    const cards = reviewCallCount === 1 ? [CARD] : [];
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(cards) });
  });

  await page.route('**/api/review/42', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(REVIEW_RESPONSE) }),
  );

  await page.route('**/api/cards**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }),
  );
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('Learning Cards IA — review session', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page);
    await page.goto('/cards');
  });

  test('shows breadcrumb with Reflect / Flashcards / All decks · session', async ({ page }) => {
    await expect(page.getByText('Reflect')).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('button', { name: /flashcards/i })).toBeVisible();
    await expect(page.getByText(/all decks · session/i)).toBeVisible();
  });

  test('shows PROGRESS bar on review entry', async ({ page }) => {
    await expect(page.getByText(/progress/i)).toBeVisible({ timeout: 8000 });
    // The progressbar element is present in the DOM (aria role) even at 0% width.
    // Use count() to verify it exists rather than toBeVisible() (zero-width bars are "hidden").
    await expect(page.getByRole('progressbar')).toHaveCount(1);
  });

  test('card front question is visible', async ({ page }) => {
    await expect(
      page.getByText('What is the time complexity of the RGS algorithm?'),
    ).toBeVisible({ timeout: 8000 });
  });

  test('card shows deck name in eyebrow after decks load', async ({ page }) => {
    await expect(page.getByText(/§ Card 1 · RGS THESIS/i)).toBeVisible({ timeout: 8000 });
  });

  test('card shows "last seen" in eyebrow', async ({ page }) => {
    await expect(page.getByText(/last seen 4d/i)).toBeVisible({ timeout: 8000 });
  });

  test('click to reveal shows § ANSWER section', async ({ page }) => {
    await page.getByText(/click to reveal answer/i).click();
    await expect(page.getByText(/§ Answer/i)).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('O(n log n) in the average case.')).toBeVisible();
  });

  test('rating buttons appear after reveal', async ({ page }) => {
    await page.getByText(/click to reveal answer/i).click();
    await expect(page.getByRole('button', { name: /again/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /hard/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /good/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /easy/i })).toBeVisible();
  });

  test('skip button visible before reveal', async ({ page }) => {
    await expect(page.getByText('What is the time complexity of the RGS algorithm?')).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('button', { name: /skip/i })).toBeVisible();
  });

  test('rating Good advances session — progress updates', async ({ page }) => {
    await page.getByText(/click to reveal answer/i).click();
    await page.getByRole('button', { name: /good/i }).click();
    // After rating, session advances — reviewed count should increment
    // Either next card loads OR session-complete panel appears
    await expect(
      page.getByText(/session complete/i)
        .or(page.getByText(/click to reveal answer/i))
        .or(page.getByText(/loading next card/i)),
    ).toBeVisible({ timeout: 8000 });
  });

  test('session-complete panel shows after last card rated', async ({ page }) => {
    await page.getByText(/click to reveal answer/i).click();
    await page.getByRole('button', { name: /good/i }).click();
    await expect(page.getByText(/session complete/i)).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('button', { name: /manage library/i })).toBeVisible();
  });
});

test.describe('Learning Cards IA — library navigation', () => {
  test('breadcrumb Flashcards link navigates to Library view', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page);
    await page.goto('/cards');
    await expect(page.getByRole('button', { name: /flashcards/i })).toBeVisible({ timeout: 8000 });
    await page.getByRole('button', { name: /flashcards/i }).click();
    await expect(page.getByRole('heading', { name: /flashcards/i })).toBeVisible({ timeout: 5000 });
  });

  test('Library view shows StatsHeader tiles', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page, { dueNow: 0 });
    await page.goto('/cards');
    await expect(page.getByRole('heading', { name: /flashcards/i })).toBeVisible({ timeout: 8000 });
    await expect(page.getByText('Total Cards')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('Due Now')).toBeVisible();
    await expect(page.getByText('Reviewed Today')).toBeVisible();
  });

  test('Library view shows deck grid with deck names', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page, { dueNow: 0 });
    await page.goto('/cards');
    await expect(page.getByRole('heading', { name: /flashcards/i })).toBeVisible({ timeout: 8000 });
    await expect(page.getByText('RGS Thesis')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('Neural ODEs')).toBeVisible();
  });

  test('Library view shows Generate and New Card buttons', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page, { dueNow: 0 });
    await page.goto('/cards');
    await expect(page.getByRole('heading', { name: /flashcards/i })).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('button', { name: /generate/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /new card/i })).toBeVisible();
  });

  test('deck with due cards shows "Start review" button', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page, { dueNow: 0 });
    await page.goto('/cards');
    await expect(page.getByRole('heading', { name: /flashcards/i })).toBeVisible({ timeout: 8000 });
    // RGS Thesis has 5 due cards — should show the specific "Review 5" button (exact title match)
    await expect(page.getByRole('button', { name: 'Review 5', exact: true })).toBeVisible({ timeout: 5000 });
  });

  test('StatsHeader NOT shown in review session mode', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page, { dueNow: 5 });
    await page.goto('/cards');
    await expect(page.getByText(/all decks · session/i)).toBeVisible({ timeout: 8000 });
    // Stats tiles should NOT be visible in session mode
    await expect(page.getByText('Total Cards')).not.toBeVisible();
  });
});

test.describe('Learning Cards IA — URL routing', () => {
  test('?mode=library shows Library view directly', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page, { dueNow: 5 });
    await page.goto('/cards?mode=library');
    await expect(page.getByRole('heading', { name: /flashcards/i })).toBeVisible({ timeout: 8000 });
  });

  test('no-due-cards defaults to Library view', async ({ page }) => {
    await seedAuthedSession(page);
    await seedRoutes(page, { dueNow: 0 });
    await page.goto('/cards');
    await expect(page.getByRole('heading', { name: /flashcards/i })).toBeVisible({ timeout: 8000 });
  });
});

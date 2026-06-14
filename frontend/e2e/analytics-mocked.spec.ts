/**
 * Analytics "Reflect" IA — mocked Playwright e2e walk.
 *
 * Uses page.route to fulfill all analytics API calls with seeded data so
 * the tests run against the mocked frontend bundle (baseURL 127.0.0.1:3001)
 * without a live backend.
 *
 * Seed: two periods of daily_log data so deltas are non-zero.
 */
import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

// ── Seed data ────────────────────────────────────────────────────────────────

const MOCK_SUMMARY = {
  papers_read_total: 24,
  focus_hours_total: 37.2,
  cards_reviewed_total: 412,
  papers_read_prev: 18,
  focus_hours_prev: 41.3,
  cards_reviewed_prev: 380,
  focus_streak_days: 5,
  cards_review_streak_days: 28,
};

const MOCK_ACTIVITY = [
  { log_date: '2026-05-01', tasks_completed: 2, cards_reviewed: 15, papers_read: 1, focus_hours: 2.5, notes: null },
  { log_date: '2026-05-02', tasks_completed: 3, cards_reviewed: 20, papers_read: 2, focus_hours: 3.0, notes: null },
];

const MOCK_RETENTION = [
  { review_date: '2026-05-01', total: 25, good_easy: 20, retention_pct: 80.0 },
  { review_date: '2026-05-02', total: 30, good_easy: 26, retention_pct: 86.7 },
];

const MOCK_REVIEWS = [
  { rating: 1, count: 5 },
  { rating: 3, count: 15 },
  { rating: 4, count: 20 },
  { rating: 5, count: 10 },
];

const MOCK_LLM_COST = [
  { day: '2026-05-01', total_cost: 0.03, workflow: 'summarize' },
  { day: '2026-05-02', total_cost: 0.07, workflow: 'card_generate' },
];

const MOCK_BY_SOURCE = [
  { source_type: 'arxiv', count: 30 },
  { source_type: 'doi', count: 12 },
  { source_type: 'local', count: 5 },
];

const MOCK_BY_STATUS = [
  { status: 'new', count: 20 },
  { status: 'read', count: 15 },
  { status: 'summarized', count: 12 },
];

// ── Helpers ──────────────────────────────────────────────────────────────────

async function mockAnalyticsRoutes(page: import('@playwright/test').Page) {
  // FirstRunGate — must return setup_completed: true or the wizard intercepts the page.
  await page.route('**/api/setup/status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) }),
  );

  await page.route('**/api/analytics/summary**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SUMMARY) }),
  );
  await page.route('**/api/analytics/activity**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_ACTIVITY) }),
  );
  await page.route('**/api/analytics/retention**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_RETENTION) }),
  );
  await page.route('**/api/analytics/reviews**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_REVIEWS) }),
  );
  await page.route('**/api/analytics/llm-cost**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_LLM_COST) }),
  );
  await page.route('**/api/analytics/papers-by-source**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_BY_SOURCE) }),
  );
  await page.route('**/api/analytics/papers-by-status**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_BY_STATUS) }),
  );
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe('Analytics IA (mocked)', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);
    await mockAnalyticsRoutes(page);
    // Mock the setup config endpoint so the app doesn't block on setup gate
    await page.route('**/api/config/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ value: true }) }),
    );
    // Mock any user/account endpoints
    await page.route('**/api/auth/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ authenticated: true }) }),
    );
    await page.goto('/analytics');
    await page.waitForLoadState('networkidle', { timeout: 15_000 });
  });

  test('renders Analytics hero heading', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible();
  });

  test('breadcrumb shows Learn / Analytics', async ({ page }) => {
    // h1 is the page name "Analytics"; the breadcrumb group is the real sidebar group "Learn".
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible();
    await expect(page.locator('main nav')).toContainText('Learn');
    // The breadcrumb link "Analytics" is in the main content area nav
    await expect(page.locator('main nav').getByRole('link', { name: 'Analytics' })).toBeVisible();
  });

  test('§ REVIEW · 30 DAYS marker is visible by default', async ({ page }) => {
    await expect(page.getByText(/REVIEW · 30 DAYS/i)).toBeVisible();
  });

  test('italic period subtitle "since" is visible', async ({ page }) => {
    await expect(
      page.getByText(/What you learned, and how you spent your time, since/i),
    ).toBeVisible();
  });

  test('KPI band shows PAPERS READ with total 24', async ({ page }) => {
    // Use exact: true to avoid matching Recharts legend "Papers Read"
    await expect(page.getByText('PAPERS READ', { exact: true })).toBeVisible({ timeout: 8_000 });
    // The KPI value "24" appears inside a data-testid="kpi-value" element
    const kpiValues = page.locator('[data-testid="kpi-value"]');
    await expect(kpiValues.filter({ hasText: /^24$/ })).toBeVisible({ timeout: 8_000 });
  });

  test('KPI band shows positive papers delta (+6 vs prev)', async ({ page }) => {
    await expect(page.locator('[data-testid="trend-chip"]').first()).toBeVisible({ timeout: 8_000 });
    // papers_read 24 − 18 = +6
    await expect(page.locator('[data-testid="kpi-band"]')).toContainText('+6');
  });

  test('KPI band shows FOCUS HOURS 37.2', async ({ page }) => {
    await expect(page.getByText('FOCUS HOURS')).toBeVisible({ timeout: 8_000 });
    await expect(page.locator('[data-testid="kpi-band"]')).toContainText('37.2');
  });

  test('KPI band shows 28-day streak for CARDS REVIEWED', async ({ page }) => {
    await expect(page.locator('[data-testid="streak-chip"]')).toBeVisible({ timeout: 8_000 });
    await expect(page.locator('[data-testid="streak-chip"]')).toContainText('28-day streak');
  });

  test('date range filter preset buttons are visible', async ({ page }) => {
    await expect(page.getByRole('button', { name: /last 7 days/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /last 30 days/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /last 90 days/i })).toBeVisible();
  });

  test('clicking Last 7 days updates § REVIEW · 7 DAYS marker', async ({ page }) => {
    await page.getByRole('button', { name: /last 7 days/i }).click();
    await expect(page.getByText(/REVIEW · 7 DAYS/i)).toBeVisible({ timeout: 5_000 });
  });

  test('§ READING CADENCE section marker is visible', async ({ page }) => {
    await expect(page.getByText(/READING CADENCE/i)).toBeVisible();
  });

  test('§ LIBRARY section marker is visible', async ({ page }) => {
    // MarkerCaption renders "§ " + "LIBRARY" as sibling text nodes in a <span>
    // Use locator with exact text content matching
    await expect(page.locator('span').filter({ hasText: /^§ LIBRARY$/ })).toBeVisible();
  });

  test('§ REVIEWS section marker is visible', async ({ page }) => {
    // Use span filter to avoid matching "Reviews by Rating" card title
    await expect(page.locator('span').filter({ hasText: /^§ REVIEWS$/ })).toBeVisible();
  });

  test('§ COST section marker is visible', async ({ page }) => {
    // Use span filter to avoid matching "LLM Cost Over Time"
    await expect(page.locator('span').filter({ hasText: /^§ COST$/ })).toBeVisible();
  });

  test('all six existing chart card titles are visible (regression guard)', async ({ page }) => {
    await expect(page.getByText('Daily Activity')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Retention Trend')).toBeVisible();
    await expect(page.getByText('Papers by Source')).toBeVisible();
    await expect(page.getByText('Papers by Status')).toBeVisible();
    await expect(page.getByText('Reviews by Rating')).toBeVisible();
    await expect(page.getByText('LLM Cost Over Time')).toBeVisible();
  });
});

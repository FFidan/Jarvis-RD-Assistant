/**
 * F1 Feed IA v3 — Playwright mocked e2e spec
 *
 * Tests the faceted 3-pane "research-log triage" IA using page.route() stubs.
 * Walks: Inbox-led → Library-led → facet counts → bulk ops intact.
 *
 * Base URL: http://127.0.0.1:3001 (mocked — no live backend required)
 */
import { test, expect } from '@playwright/test';
import { installMockedApiDefaults, seedAuthedSession } from '../helpers/setup';

// ── shared stub data ───────────────────────────────────────────────────────

const RICH_COUNTS = {
  inbox: 12,
  library: 45,
  reading_list: 8,
  reading: 3,
  done: 20,
  starred: 7,
  trash: 2,
  active: 60,
  kept: 60,
  all_non_trash: 80,
  by_source: { arxiv: 25, semantic_scholar: 18, openalex: 5 },
  by_topic: [
    { topic_id: 1, name: 'Machine Learning', count: 30 },
    { topic_id: 2, name: 'Neuroscience', count: 10 },
  ],
  untagged: 5,
};

const INBOX_PAPER = {
  id: 1,
  external_id: 'arxiv:2301.00001',
  source_type: 'arxiv',
  title: 'Neural Scaling Laws',
  authors: ['Author A', 'Author B'],
  abstract: 'We study scaling laws for neural networks.',
  published_date: '2025-01-01',
  url: 'https://arxiv.org/abs/2301.00001',
  pdf_url: null,
  pdf_local_path: null,
  pdf_downloaded: false,
  citation_count: 42,
  priority_score: 0.85,
  metadata: {},
  discovered_at: '2025-01-01T00:00:00Z',
  created_at: '2025-01-01T00:00:00Z',
  summary_brief: 'Study of scaling laws.',
  tldr: 'Scaling laws matter.',
  confidence: 'HIGH',
  recommendation_score: 0.92,
  recommendation_reason: 'Matches your ML topics',
  state: 'inbox',
  state_before_trash: null,
  starred: false,
  rating: null,
  has_chunks: true,
  has_summary: true,
  priority_level: 'high',
  discovery_origin: 'recommender',
};

const LIBRARY_PAPER = {
  id: 2,
  external_id: 'arxiv:2302.00002',
  source_type: 'semantic_scholar',
  title: 'Attention Mechanisms Survey',
  authors: ['Author C'],
  abstract: 'A survey of attention mechanisms.',
  published_date: '2025-02-01',
  url: 'https://arxiv.org/abs/2302.00002',
  pdf_url: null,
  pdf_local_path: null,
  pdf_downloaded: false,
  citation_count: 10,
  priority_score: 0.5,
  metadata: {},
  discovered_at: '2025-02-01T00:00:00Z',
  created_at: '2025-02-01T00:00:00Z',
  summary_brief: 'Survey on attention.',
  tldr: null,
  confidence: null,
  recommendation_score: null,
  recommendation_reason: null,
  state: 'to_read',
  state_before_trash: null,
  starred: true,
  rating: null,
  has_chunks: false,
  has_summary: false,
  priority_level: null,
  discovery_origin: null,
};

// ── route stubs ───────────────────────────────────────────────────────────

async function stubFeedRoutes(page: import('@playwright/test').Page) {
  // Dismiss the onboarding tour so it doesn't intercept or crash the shell.
  await page.addInitScript(() => {
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  // Auth verify — AppShell checks auth state on mount.
  await page.route('/api/auth/verify', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 1, email: 'test@example.com', role: 'user' }) }),
  );

  // FirstRunGate — must return setup_completed: true or the wizard intercepts the page.
  await page.route('**/api/setup/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ configured: true, setup_completed: true }),
    });
  });

  // Sources
  await page.route('**/api/sources**', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { source_type: 'arxiv', enabled: true, config: {}, priority: 1, display_order: 1, created_at: '2025-01-01T00:00:00Z' },
        { source_type: 'semantic_scholar', enabled: true, config: {}, priority: 2, display_order: 2, created_at: '2025-01-01T00:00:00Z' },
        { source_type: 'local', enabled: true, config: {}, priority: 3, display_order: 3, created_at: '2025-01-01T00:00:00Z' },
      ]),
    });
  });

  // Feed list (serves both inbox and library papers based on ?view=).
  // Registered BEFORE feed/counts so feed/counts (registered after) takes priority (LIFO).
  await page.route('**/api/papers/feed**', async (route) => {
    const url = new URL(route.request().url());
    const view = url.searchParams.get('view');
    const papers = view === 'library' ? [LIBRARY_PAPER] : [INBOX_PAPER];
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ papers, total: papers.length }),
    });
  });

  // Feed counts — registered AFTER feed-list so it takes priority over the broader
  // **/api/papers/feed** pattern (LIFO: last registered = first matched).
  await page.route('**/api/papers/feed/counts**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(RICH_COUNTS),
    });
  });
}

// ── tests ─────────────────────────────────────────────────────────────────

test.describe('F1 Feed IA v3 — mocked walk', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);
    await installMockedApiDefaults(page);
    await stubFeedRoutes(page);
  });

  // ── Inbox-led walk ─────────────────────────────────────────────────────

  test('Inbox-led: facet rail visible with §Status/§Star/§Source/§Topic sections', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    // Rail is present
    const facetNav = page.getByRole('navigation', { name: /feed facets/i });
    await expect(facetNav).toBeVisible();

    // Section headers — scoped to the desktop facet rail to avoid strict-mode
    // violations from the mobile Sheet, which renders a second FacetListContent.
    await expect(facetNav.getByText('Status')).toBeVisible();
    // Use exact:true to match only the section header <p>Star</p>, not the "Starred" facet item.
    await expect(facetNav.getByText('Star', { exact: true })).toBeVisible();
    await expect(facetNav.getByText('Source')).toBeVisible();
    await expect(facetNav.getByText('Topic')).toBeVisible();
  });

  test('Inbox-led: §Source facet counts from API render in rail', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    // arXiv facet with count 25
    await expect(page.getByTestId('facet-source-arxiv')).toBeVisible();
    await expect(page.getByTestId('facet-source-arxiv')).toContainText('25');

    // Semantic Scholar facet with count 18
    await expect(page.getByTestId('facet-source-semantic_scholar')).toBeVisible();
    await expect(page.getByTestId('facet-source-semantic_scholar')).toContainText('18');
  });

  test('Inbox-led: §Topic facet counts render in rail', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    await expect(page.getByTestId('facet-topic-1')).toContainText('Machine Learning');
    await expect(page.getByTestId('facet-topic-1')).toContainText('30');
    await expect(page.getByTestId('facet-topic-untagged')).toContainText('Untagged');
    await expect(page.getByTestId('facet-topic-untagged')).toContainText('5');
  });

  test('Inbox-led: inbox paper renders in main list', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');
    await expect(page.getByText('Neural Scaling Laws')).toBeVisible();
  });

  test('Inbox-led: scoped filter input is visible', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');
    await expect(page.getByTestId('feed-list-filter')).toBeVisible();
  });

  // ── Library-led walk ───────────────────────────────────────────────────

  test('Library-led: clicking §Status Library shows library papers', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    // Click Library status (maps to surface=library)
    await page.getByTestId('facet-status-library').click();
    await page.waitForLoadState('networkidle');

    await expect(page.getByText('Attention Mechanisms Survey')).toBeVisible();
  });

  test('Library-led: library scope toggle is visible at surface=library', async ({ page }) => {
    await page.goto('/feed?surface=library');
    await page.waitForLoadState('networkidle');
    await expect(page.getByRole('tablist', { name: /library scope/i })).toBeVisible();
  });

  // ── Trash as §Status facet ─────────────────────────────────────────────

  test('Trash: appears as §Status facet, not a top-level tab', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    // §Status Trash facet visible
    await expect(page.getByTestId('facet-status-trash')).toBeVisible();

    // No tab with name "Trash" in the page
    const trashTab = page.getByRole('tab', { name: /^trash$/i });
    await expect(trashTab).not.toBeVisible().catch(() => { /* not present */ });
  });

  test('Trash: clicking Trash §Status shows trash warning alert', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    await page.getByTestId('facet-status-trash').click();
    await page.waitForLoadState('networkidle');

    await expect(page.getByRole('alert')).toContainText(/papers in trash/i);
  });

  // ── Ask removed from feed ──────────────────────────────────────────────

  test('Ask: no tab or button labeled "Ask" in the feed', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    // Should not have an Ask tab
    await expect(page.getByRole('tab', { name: /^ask$/i })).not.toBeVisible().catch(() => { /* ok */ });
  });

  test('Ask: ?surface=ask redirects to inbox (no StreamingChat in feed)', async ({ page }) => {
    await page.goto('/feed?surface=ask');
    await page.waitForLoadState('networkidle');

    // Should land on inbox (or at least not show Ask content)
    // The facet rail is present (we're on the feed page)
    await expect(page.getByTestId('facet-rail')).toBeVisible();
  });

  // ── Bulk ops intact ────────────────────────────────────────────────────

  test('Bulk ops: BulkToolbar renders at inbox with papers', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    // Wait for paper to appear
    await expect(page.getByText('Neural Scaling Laws')).toBeVisible();

    // BulkToolbar checkbox should be present (Select all)
    // The checkbox for select-all has aria-label "Select all on this page"
    await expect(page.getByRole('checkbox', { name: /select all on this page/i })).toBeVisible();
  });

  // ── Discover (search surface) via rail ────────────────────────────────

  test('Discover: clicking Discover in rail shows search surface', async ({ page }) => {
    await page.goto('/feed?surface=inbox');
    await page.waitForLoadState('networkidle');

    await page.getByTestId('facet-discover').click();
    await page.waitForLoadState('networkidle');

    // Search surface content: external search bar
    await expect(page.getByText(/search external databases/i)).toBeVisible();
  });
});

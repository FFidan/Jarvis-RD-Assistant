/**
 * Feed lifecycle smoke test (Inbox → Save → Library → Star → Done
 *             → Trash → Restore → HardDelete).
 *
 * MODE: mocked — all /api/papers/* calls are intercepted; no live stack required
 * for test discovery or basic route / surface navigation checks.
 *
 * LIVE-STACK NOTE: Steps that assert the paper *disappears from a surface* after
 * a mutation depend on the query cache being invalidated and the subsequent
 * /api/papers/feed call returning the paper absent.  In mocked mode we verify
 * that the correct API endpoint was called (via waitForRequest) and that the
 * mock follow-up feed response (without the paper) is picked up by the UI.
 * Step 11 (GET /api/papers/{id} → 404) is conditional on LIVE_BACKEND=1
 * matching the intent of the sprint spec.
 *
 * Lifecycle callbacks: wired in FeedView.tsx:246-269 (forwards onSave/onSkip/onTrash/etc to FeedPaperRow).
 */

import { test, expect, type Page, type Route } from '@playwright/test';
import { installMockedApiDefaults, seedAuthedSession } from './helpers/setup';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAPER_ID = 42;
const PAPER_TITLE = 'Lifecycle Test Paper';

/** Toggle: set LIVE_BACKEND=1 to run step 8 (GET /api/papers/{id} → 404). */
const LIVE_BACKEND = process.env.LIVE_BACKEND === '1';

// ---------------------------------------------------------------------------
// Mock data factories
// ---------------------------------------------------------------------------

function makeUserState(overrides: Partial<{
  saved: boolean; dismissed: boolean; starred: boolean; archived: boolean;
  status: string;
}> = {}) {
  return {
    status: 'new',
    saved: false,
    dismissed: false,
    starred: false,
    archived: false,
    preference: 'none',
    rating: null,
    user_notes: null,
    flagged: false,
    updated_at: null,
    ...overrides,
  };
}

function makePaper(
  overrides: Partial<{
    user_state: ReturnType<typeof makeUserState>;
    state: string;
    state_before_trash: string | null;
    starred: boolean;
  }> = {},
) {
  return {
    id: PAPER_ID,
    external_id: 'lifecycle-test-0042',
    title: PAPER_TITLE,
    authors: ['Alice, A.', 'Bob, B.'],
    abstract: 'Abstract text for lifecycle smoke test.',
    source_type: 'arxiv',
    url: 'https://arxiv.org/abs/lifecycle.0042',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    user_status: 'new',
    // FeedPaper.state is required by the API contract (types/paper.ts:149) and drives
    // which lifecycle controls FeedPaperRow renders (FeedPaperRow.tsx:97).
    state: 'inbox',
    state_before_trash: null,
    user_state: makeUserState(),
    published_date: '2026-01-01',
    discovered_at: '2026-01-10T08:00:00Z',
    created_at: '2026-01-10T08:00:00Z',
    priority_score: 0.9,
    citation_count: 0,
    metadata: {},
    confidence: 'HIGH',
    tldr: 'A paper for lifecycle testing.',
    summary_brief: null,
    has_chunks: false,
    has_summary: false,
    discovery_origin: 'user_initiated',
    starred: false,
    archived: false,
    rating: null,
    ...overrides,
  };
}

function feedResponse(papers: ReturnType<typeof makePaper>[]) {
  return { papers, total: papers.length };
}

const emptyCounts = {
  inbox: 0, library: 0, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0,
  active: 0, kept: 0, all_non_trash: 0, by_source: {}, by_topic: [], untagged: 0,
};
const inboxCounts = { ...emptyCounts, inbox: 1 };
const libraryCounts = { ...emptyCounts, library: 1 };
const trashCounts = { ...emptyCounts, trash: 1 };

// ---------------------------------------------------------------------------
// Route helpers
// ---------------------------------------------------------------------------

/**
 * Install a stubbed /api/papers/feed response and a stubbed
 * /api/papers/feed/counts response.  The `feedBody` callback is called
 * each time the feed is requested, allowing state transitions.
 */
async function routeFeedAndCounts(
  page: Page,
  getFeedBody: () => object,
  getCountsBody: () => object,
) {
  await page.route('**/api/papers/feed/counts', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(getCountsBody()),
    });
  });

  await page.route('**/api/papers/feed**', async (route: Route) => {
    // Don't intercept counts (already handled above via the more-specific pattern)
    if (route.request().url().includes('/feed/counts')) {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(getFeedBody()),
    });
  });

  await page.route('**/api/topics**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });

  await page.route('**/api/sources**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
}

/** Stub a single PUT lifecycle endpoint and return its route. */
async function stubLifecycleEndpoint(
  page: Page,
  urlPattern: string | RegExp,
  responseBody: object,
) {
  await page.route(urlPattern, async (route: Route) => {
    if (route.request().method() !== 'PUT') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(responseBody),
    });
  });
}

/** Stub DELETE endpoint (hard delete). */
async function stubDeleteEndpoint(page: Page, paperId: number) {
  await page.route(`**/api/papers/${paperId}`, async (route: Route) => {
    if (route.request().method() !== 'DELETE') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ deleted: 1 }),
    });
  });
}

// ---------------------------------------------------------------------------
// Reachability guard — an unreachable dashboard is a failure, not a skip
// ---------------------------------------------------------------------------

// This suite runs in a required CI check. Skipping when the preview server is
// down would let that check report success having executed zero tests, so a
// server that failed to start must fail the run loudly instead.
async function assertDashboardReachable(page: Page) {
  let resp;
  try {
    resp = await page.request.get('/', { timeout: 3_000 });
  } catch (err) {
    throw new Error(`Dashboard unreachable: ${(err as Error).message}`);
  }
  if (!resp.ok()) {
    throw new Error(`Dashboard unreachable (status ${resp.status()})`);
  }
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

test.describe('Feed — full lifecycle smoke', () => {
  test.setTimeout(60_000);

  test.beforeEach(async ({ page }) => {
    await assertDashboardReachable(page);
    await seedAuthedSession(page);
    await installMockedApiDefaults(page);
    // FirstRunGate — must return setup_completed: true or the wizard intercepts all routes.
    await page.route('**/api/setup/status', async (route: Route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) });
    });
  });

  // Steps 0-2 are covered by the enabled feed/feed-ia-v3.spec.ts surface and facet receipts.

  // ── Step 3: Empty state messages render per surface ──────────────────────

  test('3. Empty states render for each surface', async ({ page }) => {
    await routeFeedAndCounts(
      page,
      () => feedResponse([]),
      () => emptyCounts,
    );

    await page.goto('/feed?surface=inbox');
    await expect(page.getByText('Inbox is empty')).toBeVisible({ timeout: 10_000 });

    await page.goto('/feed?surface=library');
    await expect(
      page.getByRole('heading', { name: 'No papers in your library' }),
    ).toBeVisible({ timeout: 10_000 });

    await page.goto('/feed?surface=trash');
    await expect(page.getByText('Trash is empty')).toBeVisible({ timeout: 10_000 });
  });

  // ── Step 4: Mark Reading (currently wired) ──────────────────────────────

  test('4. Mark Reading fires PUT /api/papers/{id}/reading', async ({ page }) => {
    // The control under test is FeedPaperRow's "Mark Reading" button, which renders
    // only for state === 'to_read' (FeedPaperRow.tsx:336,345); a default 'inbox'
    // paper never shows it.
    await routeFeedAndCounts(
      page,
      () => feedResponse([makePaper({ state: 'to_read' })]),
      () => libraryCounts,
    );

    // Stub the reading-transition endpoint (markReading in lib/api/papers.ts:157)
    await page.route(`**/api/papers/${PAPER_ID}/reading`, async (route: Route) => {
      if (route.request().method() !== 'PUT') { await route.continue(); return; }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', paper_id: PAPER_ID }) });
    });

    await page.goto('/feed?surface=library');
    await expect(page.getByText(PAPER_TITLE)).toBeVisible({ timeout: 10_000 });

    // Exact aria-label="Mark <title> as reading" (FeedPaperRow.tsx:345), wired by
    // FeedView via onMarkReading (FeedView.tsx:254,370).  A missing button is a
    // regression, not a pending-wiring condition: assert it, never skip.
    const markReadingBtn = page.getByRole('button', {
      name: `Mark ${PAPER_TITLE} as reading`,
      exact: true,
    });
    await expect(markReadingBtn).toBeVisible({ timeout: 10_000 });

    // Wait on the exact endpoint path so an unrelated paper PUT cannot satisfy
    // the assertion, then pin URL, method, and (empty) payload to the contract.
    const [request] = await Promise.all([
      page.waitForRequest((req) =>
        new URL(req.url()).pathname === `/api/papers/${PAPER_ID}/reading` && req.method() === 'PUT',
      ),
      markReadingBtn.click(),
    ]);
    expect(new URL(request.url()).pathname).toBe(`/api/papers/${PAPER_ID}/reading`);
    expect(request.method()).toBe('PUT');
    expect(request.postData()).toBeNull();
  });

  // ── Step 5 (LIFECYCLE_WIRED): Save from Inbox ────────────────────────────

  test('5. [LIFECYCLE_WIRED] Save button in Inbox fires /save and paper disappears', async ({ page }) => {
    let savedState = false;

    await stubLifecycleEndpoint(
      page,
      `**/api/papers/${PAPER_ID}/save`,
      { status: 'ok', paper_id: PAPER_ID },
    );

    await routeFeedAndCounts(
      page,
      () => savedState ? feedResponse([]) : feedResponse([makePaper()]),
      () => savedState ? libraryCounts : inboxCounts,
    );

    await page.goto('/feed?surface=inbox');
    await expect(page.getByText(PAPER_TITLE)).toBeVisible({ timeout: 10_000 });

    const saveBtn = page.getByRole('button', { name: `Save ${PAPER_TITLE}` });
    await expect(saveBtn).toBeVisible({ timeout: 5_000 });

    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes(`/api/papers/${PAPER_ID}/save`) && r.method() === 'PUT'),
      (async () => { savedState = true; await saveBtn.click(); })(),
    ]);
    expect(req.url()).toContain(`/api/papers/${PAPER_ID}/save`);

    // Paper should disappear from Inbox after cache invalidation
    await expect(page.getByText(PAPER_TITLE)).not.toBeVisible({ timeout: 10_000 });
  });

  // ── Step 6 (LIFECYCLE_WIRED): Star in Library ────────────────────────────

  test('6. Star in Library fires /star', async ({ page }) => {
    await stubLifecycleEndpoint(
      page,
      `**/api/papers/${PAPER_ID}/star`,
      { status: 'ok', paper_id: PAPER_ID },
    );

    await routeFeedAndCounts(
      page,
      () => feedResponse([makePaper({ state: 'to_read' })]),
      () => libraryCounts,
    );

    await page.goto('/feed?surface=library');
    await expect(page.getByText(PAPER_TITLE)).toBeVisible({ timeout: 10_000 });

    const starBtn = page.getByRole('button', { name: `Star ${PAPER_TITLE}` });
    await expect(starBtn).toBeVisible({ timeout: 5_000 });

    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes(`/api/papers/${PAPER_ID}/star`) && r.method() === 'PUT'),
      starBtn.click(),
    ]);
    expect(req.url()).toContain(`/api/papers/${PAPER_ID}/star`);
  });

  // ── Step 7 (LIFECYCLE_WIRED): Archive in Library ─────────────────────────

  test('7. Mark done in Library fires /done', async ({ page }) => {
    let doneState = false;

    await stubLifecycleEndpoint(
      page,
      `**/api/papers/${PAPER_ID}/done`,
      { status: 'ok', paper_id: PAPER_ID },
    );

    await routeFeedAndCounts(
      page,
      () => doneState ? feedResponse([]) : feedResponse([makePaper({ state: 'to_read' })]),
      () => doneState ? { ...libraryCounts, library: 0, done: 1 } : libraryCounts,
    );

    await page.goto('/feed?surface=library');
    await expect(page.getByText(PAPER_TITLE)).toBeVisible({ timeout: 10_000 });

    const doneBtn = page.getByRole('button', { name: `Mark ${PAPER_TITLE} as done` });
    await expect(doneBtn).toBeVisible({ timeout: 5_000 });

    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes(`/api/papers/${PAPER_ID}/done`) && r.method() === 'PUT'),
      (async () => { doneState = true; await doneBtn.click(); })(),
    ]);
    expect(req.url()).toContain(`/api/papers/${PAPER_ID}/done`);

    // Paper should leave default Library view
    await expect(page.getByText(PAPER_TITLE)).not.toBeVisible({ timeout: 10_000 });

  });

  // ── Step 8 (LIFECYCLE_WIRED): Dismiss from Library ───────────────────────

  test('8. Trash fires /trash and row vanishes', async ({ page }) => {
    let trashedState = false;

    await stubLifecycleEndpoint(
      page,
      `**/api/papers/${PAPER_ID}/trash`,
      { status: 'ok', paper_id: PAPER_ID },
    );

    await routeFeedAndCounts(
      page,
      () => trashedState ? feedResponse([]) : feedResponse([makePaper({ state: 'to_read' })]),
      () => trashedState ? { ...libraryCounts, library: 0, trash: 1 } : libraryCounts,
    );

    await page.goto('/feed?surface=library');
    await expect(page.getByText(PAPER_TITLE)).toBeVisible({ timeout: 10_000 });

    const trashBtn = page.getByRole('button', { name: `Trash ${PAPER_TITLE}` });
    await expect(trashBtn).toBeVisible({ timeout: 5_000 });

    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes(`/api/papers/${PAPER_ID}/trash`) && r.method() === 'PUT'),
      (async () => { trashedState = true; await trashBtn.click(); })(),
    ]);
    expect(req.url()).toContain(`/api/papers/${PAPER_ID}/trash`);

    // Row vanishes from Library
    await expect(page.getByText(PAPER_TITLE)).not.toBeVisible({ timeout: 10_000 });

  });

  // ── Step 9 (LIFECYCLE_WIRED): Restore from Trash ─────────────────────────

  test('9. Restore from Trash fires /restore', async ({ page }) => {
    let restoredState = false;

    await stubLifecycleEndpoint(
      page,
      `**/api/papers/${PAPER_ID}/restore`,
      { status: 'ok', paper_id: PAPER_ID },
    );

    await routeFeedAndCounts(
      page,
      () => restoredState ? feedResponse([]) : feedResponse([makePaper({ state: 'trash', state_before_trash: 'to_read' })]),
      () => restoredState ? { ...libraryCounts } : trashCounts,
    );

    await page.goto('/feed?surface=trash');
    await expect(page.getByText(PAPER_TITLE)).toBeVisible({ timeout: 10_000 });

    const restoreBtn = page.getByRole('button', { name: `Restore ${PAPER_TITLE}` });
    await expect(restoreBtn).toBeVisible({ timeout: 5_000 });

    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes(`/api/papers/${PAPER_ID}/restore`) && r.method() === 'PUT'),
      (async () => { restoredState = true; await restoreBtn.click(); })(),
    ]);
    expect(req.url()).toContain(`/api/papers/${PAPER_ID}/restore`);

    // Row vanishes from Trash
    await expect(page.getByText(PAPER_TITLE)).not.toBeVisible({ timeout: 10_000 });
  });

  // ── Step 10 (LIFECYCLE_WIRED): Hard delete from Trash ────────────────────

  test('10. Hard-delete from Trash shows modal and fires DELETE', async ({ page }) => {
    let deletedState = false;

    await stubDeleteEndpoint(page, PAPER_ID);

    await routeFeedAndCounts(
      page,
      () => deletedState ? feedResponse([]) : feedResponse([makePaper({ state: 'trash', state_before_trash: 'to_read' })]),
      () => deletedState ? emptyCounts : trashCounts,
    );

    await page.goto('/feed?surface=trash');
    await expect(page.getByText(PAPER_TITLE)).toBeVisible({ timeout: 10_000 });

    const hardDeleteBtn = page.getByRole('button', { name: `Permanently delete ${PAPER_TITLE}` });
    await expect(hardDeleteBtn).toBeVisible({ timeout: 5_000 });
    await hardDeleteBtn.click();

    await expect(page.getByRole('alertdialog')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByRole('heading', { name: 'Permanently delete this paper?' })).toBeVisible();

    const deleteBtn = page.getByRole('button', { name: 'Delete', exact: true });
    await expect(deleteBtn).toBeEnabled({ timeout: 3_000 });

    // Click and wait for DELETE request
    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes(`/api/papers/${PAPER_ID}`) && r.method() === 'DELETE'),
      (async () => { deletedState = true; await deleteBtn.click(); })(),
    ]);
    expect(req.url()).toContain(`/api/papers/${PAPER_ID}`);

    // Modal closes
    await expect(page.getByRole('alertdialog')).not.toBeVisible({ timeout: 5_000 });

    // Row vanishes from Trash
    await expect(page.getByText(PAPER_TITLE)).not.toBeVisible({ timeout: 10_000 });
  });

  // ── Step 11 (LIVE_BACKEND): Verify 404 after hard delete ─────────────────

  test('11. [LIVE_BACKEND] GET /api/papers/{id} returns 404 after hard delete', async ({ page, request }) => {
    if (!LIVE_BACKEND) {
      test.skip(true, 'Skipped: requires live backend. Set LIVE_BACKEND=1 and ensure stack is running.');
    }

    // This test is a live-backend integration assertion.
    // It expects a previously hard-deleted paper (PAPER_ID must exist in the DB)
    // to return 404. Run with a seeded paper and LIVE_BACKEND=1 for full integration.
    const resp = await request.get(`/api/papers/${PAPER_ID}`);
    expect(resp.status()).toBe(404);
  });
});

/**
 * paper-detail-3pane.spec.ts
 *
 * Playwright-mocked e2e walkthrough of the Paper Detail 3-pane IA redesign (F2).
 *
 * All API calls are fulfilled via page.route() mocks — no backend required.
 * Test uses seedAuthedSession + baseURL http://127.0.0.1:3001.
 *
 * Coverage:
 *  - Left rail: TOC visible with section labels + pipeline status on desktop
 *  - Center: breadcrumb, title, all §-sections rendered without tab clicks
 *  - Right rail: ActionsSidebar present
 *  - Lazy chunks: collapsed by default, expand on click
 *  - Mobile: both Sheet triggers present (Contents + Actions) on small viewport
 *  - Breadcrumb score: NOT rendered when summary has no recommendation_score
 */
import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

// ── Mock data ──────────────────────────────────────────────────────────────

const mockPaperDetail = {
  paper: {
    id: 1,
    title: 'Attention Is All You Need',
    authors: ['Vaswani, A.', 'Shazeer, N.', 'Parmar, N.'],
    abstract: 'The dominant sequence transduction models are based on complex architectures.',
    source_type: 'arxiv',
    url: 'https://arxiv.org/abs/1706.03762',
    pdf_url: 'https://arxiv.org/pdf/1706.03762',
    pdf_local_path: '/data/pdfs/1706.03762.pdf',
    pdf_downloaded: true,
    published_date: '2017-06-12',
    discovered_at: '2024-01-01T00:00:00Z',
    created_at: '2024-01-01T00:00:00Z',
    citation_count: 95000,
    priority_score: 0.95,
    metadata: {},
    external_id: '1706.03762',
  },
  summary: {
    id: 1,
    paper_id: 1,
    summary_brief: 'BRIEF_TEXT: Introduces the Transformer architecture based solely on attention mechanisms.',
    summary_detailed:
      'DETAILED_TEXT: This paper proposes the Transformer, a model architecture eschewing recurrence.',
    tldr: 'TLDR_TEXT: Transformers are attention-only.',
    methodology: 'METHODOLOGY_TEXT: Self-attention mechanism with multi-head attention.',
    limitations: 'LIMITATIONS_TEXT: Quadratic complexity with sequence length.',
    key_findings: [
      {
        finding: 'FINDING_TEXT: Transformer outperforms RNN-based models on translation tasks',
        quote: 'The Transformer achieves 28.4 BLEU on the WMT 2014 English-to-German translation task.',
        page_number: 8,
        chunk_id: null,
        verified: true,
        snapshot_path: null,
      },
    ],
    cross_references: [
      {
        related_paper_id: 99,
        relationship: 'extends',
        explanation: 'CROSSREF_TEXT: Builds on sequence-to-sequence learning with attention.',
        related_quote: null,
      },
    ],
    confidence: 'HIGH',
    summary_verified: true,
    llm_model: 'test-model',
    created_at: '2024-01-01T00:00:00Z',
  },
  chunks: [
    {
      id: 1,
      paper_id: 1,
      chunk_index: 0,
      content: 'CHUNK_0_CONTENT: The dominant sequence transduction models.',
      page_number: 1,
      start_char: 0,
      end_char: 100,
      embedding_id: null,
      created_at: '2024-01-01T00:00:00Z',
    },
    {
      id: 2,
      paper_id: 1,
      chunk_index: 1,
      content: 'CHUNK_1_CONTENT: We propose a new simple network architecture.',
      page_number: 2,
      start_char: 100,
      end_char: 200,
      embedding_id: null,
      created_at: '2024-01-01T00:00:00Z',
    },
  ],
  user_state: {
    state: 'reading',
    state_before_trash: null,
    starred: true,
    rating: 4,
    user_notes: 'Foundational paper for modern NLP.',
    flagged: false,
    updated_at: '2024-01-01T00:00:00Z',
  },
  has_project_links: true,
};

const mockContradictions = {
  total: 0,
  contradictions: [],
};

// ── Test setup ─────────────────────────────────────────────────────────────

test.beforeEach(async ({ page }) => {
  await seedAuthedSession(page);

  // FirstRunGate — must return setup_completed: true or the wizard intercepts the page.
  await page.route('**/api/setup/status', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) });
  });

  // Paper detail
  await page.route('**/api/papers/1', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockPaperDetail),
      });
    } else {
      await route.continue();
    }
  });

  // Contradictions
  await page.route('**/api/contradictions**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockContradictions),
    });
  });

  // Notes — user
  await page.route(
    (url) => url.pathname === '/api/papers/1/notes' && url.searchParams.get('source') === 'user',
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    },
  );

  // Notes — zotero
  await page.route(
    (url) => url.pathname === '/api/papers/1/notes' && url.searchParams.get('source') === 'zotero',
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    },
  );

  // Decks
  await page.route('**/api/decks**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });

  // Zotero linkage — ZoteroPanel fetches this on mount; left unmocked it never
  // resolves, so waitForLoadState('networkidle') stalls and every assertion times out.
  await page.route('**/api/papers/1/zotero', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        zotero_item_key: null,
        zotero_citation_key: null,
        zotero_last_pushed_at: null,
      }),
    });
  });
});

// ── Desktop 3-pane tests ───────────────────────────────────────────────────

test.describe('Paper Detail 3-pane — desktop', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
  });

  test('left rail: TOC section labels visible', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    const nav = page.getByRole('navigation', { name: 'Paper navigation' });
    await expect(nav).toBeVisible({ timeout: 8000 });

    await expect(nav.getByText('Brief')).toBeVisible();
    await expect(nav.getByText('Detailed Summary')).toBeVisible();
    await expect(nav.getByText('Methodology')).toBeVisible();
    await expect(nav.getByText('Limitations')).toBeVisible();
    await expect(nav.getByText('Evidence')).toBeVisible();
    await expect(nav.getByText('Cross-references')).toBeVisible();
    await expect(nav.getByText('Your Notes')).toBeVisible();
    await expect(nav.getByText('Source Passages')).toBeVisible();
  });

  test('left rail: pipeline status shows all steps complete', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    const nav = page.getByRole('navigation', { name: 'Paper navigation' });
    await expect(nav).toBeVisible({ timeout: 8000 });

    // § Pipeline header
    await expect(nav.getByText('§ Pipeline')).toBeVisible();
    // All steps complete
    await expect(nav.getByText('Downloaded')).toBeVisible();
    await expect(nav.getByText('2 passages')).toBeVisible();
    await expect(nav.getByText('Summarized')).toBeVisible();
  });

  test('center: breadcrumb shows Library / state / title', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Scope to breadcrumb nav to avoid strict-mode violation — "Library" also
    // appears in the sidebar link and potentially the page heading.
    const breadcrumb = page.getByRole('navigation', { name: /breadcrumb/i });
    await expect(breadcrumb.getByText('Library').first()).toBeVisible({ timeout: 8000 });
    await expect(page.getByText('reading')).toBeVisible();
  });

  test('center: title rendered as heading', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(
      page.getByRole('heading', { name: 'Attention Is All You Need' }),
    ).toBeVisible({ timeout: 8000 });
  });

  test('center: brief summary visible without any tab click', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(
      page.getByText(/BRIEF_TEXT: Introduces the Transformer architecture/),
    ).toBeVisible({ timeout: 8000 });
  });

  test('center: detailed summary visible without any tab click', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(
      page.getByText(/DETAILED_TEXT: This paper proposes the Transformer/),
    ).toBeVisible({ timeout: 8000 });
  });

  test('center: TL;DR band visible', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(page.getByText('TLDR_TEXT: Transformers are attention-only.')).toBeVisible({
      timeout: 8000,
    });
  });

  test('center: methodology visible', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(
      page.getByText(/METHODOLOGY_TEXT: Self-attention mechanism/),
    ).toBeVisible({ timeout: 8000 });
  });

  test('center: limitations visible', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(page.getByText(/LIMITATIONS_TEXT: Quadratic complexity/)).toBeVisible({
      timeout: 8000,
    });
  });

  test('center: key finding text visible', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(
      page.getByText(/FINDING_TEXT: Transformer outperforms RNN/),
    ).toBeVisible({ timeout: 8000 });
  });

  test('center: cross-reference explanation visible', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(
      page.getByText(/CROSSREF_TEXT: Builds on sequence-to-sequence/),
    ).toBeVisible({ timeout: 8000 });
  });

  test('center: chunks collapsed by default, expand on click', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Chunk content NOT visible initially (LazyChunksSection is collapsed)
    await expect(page.getByText(/CHUNK_0_CONTENT/)).not.toBeVisible();

    // Click expand toggle — shows the chunk list
    const toggle = page.getByTestId('chunks-expand-toggle');
    await toggle.click();

    // ChunksTab renders but each ChunkItem is individually collapsed.
    // Click the first chunk item header to expand its content.
    await page.getByText(/Chunk 0/).click();

    // Now content is visible
    await expect(page.getByText(/CHUNK_0_CONTENT/)).toBeVisible({ timeout: 5000 });
  });

  test('center: Ask This Paper section visible at the bottom', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Section heading in scrolling column
    await expect(page.getByRole('heading', { name: 'Ask This Paper' })).toBeVisible({
      timeout: 8000,
    });
    // RAGChatSection content
    await expect(page.getByText('Ask about this paper')).toBeVisible();
  });

  test('right rail: ActionsSidebar Analyze Paper button visible', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await expect(page.getByRole('button', { name: /Analyze Paper/ })).toBeVisible({
      timeout: 8000,
    });
  });

  test('score badge NOT rendered (no recommendation_score on paper detail)', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Use heading role to avoid strict-mode: title appears in both h1 and breadcrumb span.
    await expect(page.getByRole('heading', { name: 'Attention Is All You Need' })).toBeVisible({ timeout: 8000 });
    // Score badge must not appear (surface-aware rule: no fabricated score)
    await expect(page.getByText(/^Score \d+$/)).not.toBeVisible();
  });

  test('TOC navigate: clicking Brief button scrolls to #section-brief', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    const nav = page.getByRole('navigation', { name: 'Paper navigation' });
    await expect(nav).toBeVisible({ timeout: 8000 });

    // Click Brief in TOC — should set aria-current on the button
    await nav.getByRole('button', { name: 'Brief' }).click();

    // The section anchor exists
    await expect(page.locator('#section-brief')).toBeAttached();
  });
});

// ── Mobile Sheet tests ─────────────────────────────────────────────────────

test.describe('Paper Detail 3-pane — mobile', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
  });

  test('Contents sheet trigger visible on mobile', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Use heading role to avoid strict-mode: title appears in both h1 and breadcrumb span.
    await expect(page.getByRole('heading', { name: 'Attention Is All You Need' })).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('button', { name: /Contents/ })).toBeVisible();
  });

  test('Actions sheet trigger visible on mobile', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Use heading role to avoid strict-mode: title appears in both h1 and breadcrumb span.
    await expect(page.getByRole('heading', { name: 'Attention Is All You Need' })).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('button', { name: /Actions/ })).toBeVisible();
  });

  test('opening Contents sheet shows TOC sections', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: /Contents/ }).click();

    // Sheet should open and show section nav — scope to the opened dialog to avoid
    // matching the same labels in the hidden desktop TOC rail (hidden md:flex).
    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText('Brief')).toBeVisible({ timeout: 5000 });
    await expect(sheet.getByText('Methodology')).toBeVisible();
  });

  test('opening Actions sheet shows action rail', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: /Actions/ }).click();

    await expect(page.getByRole('button', { name: /Analyze Paper/ })).toBeVisible({
      timeout: 5000,
    });
  });
});

/**
 * paper-detail-reading.spec.ts
 *
 * Playwright-mocked e2e walkthrough of the reading-first Paper Detail layout.
 *
 * All API calls are fulfilled via page.route() mocks — no backend required.
 * Test uses seedAuthedSession + baseURL http://127.0.0.1:3001.
 *
 * Coverage:
 *  - Reading column: breadcrumb, title, all §-sections rendered without tabs
 *  - Contents: docked on demand, with section labels, pipeline status and
 *    scroll-spy following the reader
 *  - Actions: docked on demand; an analyzed paper gets a status, not a CTA
 *  - Related work: References and Cited by resolve from the citation graph
 *  - Evidence anchors: a page chip jumps the reader to the PDF section and
 *    asks it for that page and quote
 *  - Lazy chunks: collapsed by default, expand on click
 *  - Narrow viewport: Contents and Actions open as sheets instead
 *  - Breadcrumb score: NOT rendered when summary has no recommendation_score
 */
import { test, expect } from '@playwright/test';
import { installMockedApiDefaults, seedAuthedSession } from './helpers/setup';
import { PDF_GOTO_EVENT, type PdfGotoDetail } from '../src/lib/pdf-events';

declare global {
  interface WindowEventMap {
    [PDF_GOTO_EVENT]: CustomEvent<PdfGotoDetail>;
  }
  interface Window {
    /** Evidence-anchor requests this spec records in the page, in order. */
    pdfGotoRequests?: PdfGotoDetail[];
  }
}

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
    discovery_origin: 'user_initiated',
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
    relevance_notes: null,
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

// Paper 1 cites paper 2; paper 3 cites paper 1.
const mockCitationGraph = {
  nodes: [
    { id: 1, title: 'Attention Is All You Need', citation_count: 95000, published_date: '2017-06-12', is_stub: false },
    { id: 2, title: 'REFERENCE_TITLE: Sequence to Sequence Learning', citation_count: 20000, published_date: '2014-09-10', is_stub: false },
    { id: 3, title: 'CITEDBY_TITLE: BERT', citation_count: 80000, published_date: '2018-10-11', is_stub: true },
  ],
  edges: [
    { source: 1, target: 2, is_influential: true, context: null },
    { source: 3, target: 1, is_influential: false, context: null },
  ],
};

// ── Test setup ─────────────────────────────────────────────────────────────

test.beforeEach(async ({ page }) => {
  await seedAuthedSession(page);
    await installMockedApiDefaults(page);

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

  // The cross-referenced paper, so its link can name its target
  await page.route('**/api/papers/99', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        paper: { ...mockPaperDetail.paper, id: 99, title: 'RELATED_TITLE: Neural Machine Translation' },
        summary: null,
        chunks: [],
        user_state: null,
      }),
    });
  });

  // Citation graph — Related work reads References / Cited by from it
  await page.route('**/api/citations/graph**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockCitationGraph),
    });
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

// ── Desktop reading-layout tests ───────────────────────────────────────────

test.describe('Paper Detail reading layout — desktop', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
  });

  /** Contents and Actions are docked away until the reader asks for them. */
  async function openDock(page: import('@playwright/test').Page, testId: string) {
    await expect(page.getByTestId(testId)).toBeVisible({ timeout: 8000 });
    await page.getByTestId(testId).click();
  }

  test('Contents: section labels visible once docked', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Nothing is docked by default — the reading column owns the width.
    await expect(page.getByRole('navigation', { name: 'Paper navigation' })).toBeHidden();

    await openDock(page, 'toc-dock-toggle');
    const nav = page.getByRole('navigation', { name: 'Paper navigation' });
    await expect(nav).toBeVisible({ timeout: 8000 });

    await expect(nav.getByText('Brief')).toBeVisible();
    await expect(nav.getByText('Detailed Summary')).toBeVisible();
    await expect(nav.getByText('Methodology')).toBeVisible();
    await expect(nav.getByText('Limitations')).toBeVisible();
    await expect(nav.getByText('Evidence')).toBeVisible();
    await expect(nav.getByText('Related work')).toBeVisible();
    await expect(nav.getByText('Your Notes')).toBeVisible();
    await expect(nav.getByText('Source Passages')).toBeVisible();
  });

  test('Contents: pipeline status shows all steps complete', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');
    await openDock(page, 'toc-dock-toggle');

    const nav = page.getByRole('navigation', { name: 'Paper navigation' });
    await expect(nav).toBeVisible({ timeout: 8000 });

    // Pipeline steps header
    await expect(nav.getByText('Processing steps')).toBeVisible();
    // All steps complete
    await expect(nav.getByText('Downloaded')).toBeVisible();
    await expect(nav.getByText('2 passages')).toBeVisible();
    await expect(nav.getByText('Summarized')).toBeVisible();
  });

  test('Contents: scroll-spy follows the reader to the section they picked', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');
    await openDock(page, 'toc-dock-toggle');

    const nav = page.getByRole('navigation', { name: 'Paper navigation' });
    await expect(nav.locator('[data-toc-id="section-brief"]')).toHaveAttribute(
      'aria-current',
      'location',
      { timeout: 8000 },
    );

    await nav.getByRole('button', { name: /Methodology/ }).click();

    await expect(nav.locator('[data-toc-id="section-methodology"]')).toHaveAttribute(
      'aria-current',
      'location',
      { timeout: 8000 },
    );
    await expect(nav.locator('[data-toc-id="section-brief"]')).not.toHaveAttribute(
      'aria-current',
      'location',
    );
  });

  test('center: breadcrumb shows Papers / state / title', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Scope to breadcrumb nav to avoid a strict-mode violation — "Papers" also
    // names the sidebar link to the paper list.
    const breadcrumb = page.getByRole('navigation', { name: /breadcrumb/i });
    await expect(breadcrumb.getByText('Papers').first()).toBeVisible({ timeout: 8000 });
    await expect(breadcrumb.getByText('Reading')).toBeVisible();
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

  test('related work: References and Cited by resolve, above the similarity list', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    const relatedWork = page.getByTestId('related-work');
    // Each row is checked inside its own list, so the two directions cannot be
    // exchanged without failing here.
    const references = relatedWork.getByTestId('citation-references');
    const citedBy = relatedWork.getByTestId('citation-cited-by');
    await expect(references.getByText('References')).toBeVisible({ timeout: 8000 });
    await expect(
      references.getByRole('link', { name: 'REFERENCE_TITLE: Sequence to Sequence Learning' }),
    ).toHaveAttribute('href', '/paper/2');
    await expect(references.getByRole('link', { name: 'CITEDBY_TITLE: BERT' })).toHaveCount(0);
    await expect(citedBy.getByText('Cited by')).toBeVisible();
    await expect(
      citedBy.getByRole('link', { name: 'CITEDBY_TITLE: BERT' }),
    ).toHaveAttribute('href', '/paper/3');
    await expect(
      citedBy.getByRole('link', { name: 'REFERENCE_TITLE: Sequence to Sequence Learning' }),
    ).toHaveCount(0);
    // A paper known only from a bibliography says so.
    await expect(citedBy.getByText('not in your library')).toBeVisible();

    // The semantic-similarity list stays separate, and its link names its target.
    await expect(page.getByText('Similar in your library')).toBeVisible();
    await expect(
      page.getByText(/CROSSREF_TEXT: Builds on sequence-to-sequence/),
    ).toBeVisible();
    await expect(
      page.getByRole('link', { name: 'RELATED_TITLE: Neural Machine Translation' }),
    ).toBeVisible({ timeout: 8000 });
  });

  test('evidence anchor: the page chip jumps to the reader and asks for that page and quote', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await page.evaluate((eventName) => {
      window.pdfGotoRequests = [];
      window.addEventListener(eventName, (event) => {
        window.pdfGotoRequests?.push(event.detail);
      });
    }, PDF_GOTO_EVENT);

    await page
      .getByRole('button', { name: 'Open page 8 in the PDF reader' })
      .click();

    await expect
      .poll(() => page.evaluate(() => window.pdfGotoRequests))
      .toEqual([
        {
          page: 8,
          quote:
            'The Transformer achieves 28.4 BLEU on the WMT 2014 English-to-German translation task.',
        },
      ]);
    // The reader section is what the chip scrolled the column to.
    await expect(page.locator('#section-pdf')).toBeInViewport({ timeout: 8000 });
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
    await page.getByText(/Passage 0/).click();

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

  test('Actions: an analyzed paper gets a status line, not a call to action', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');
    await openDock(page, 'actions-dock-toggle');

    const dock = page.getByTestId('actions-dock');
    await expect(dock.getByRole('heading', { name: 'Actions' })).toBeVisible({ timeout: 8000 });
    await expect(dock.getByText(/Analyzed — passages extracted/)).toBeVisible();
    await expect(dock.getByRole('button', { name: /Analyze Paper/ })).toBeHidden();
  });

  test('score badge NOT rendered (no recommendation_score on paper detail)', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Use heading role to avoid strict-mode: title appears in both h1 and breadcrumb span.
    await expect(page.getByRole('heading', { name: 'Attention Is All You Need' })).toBeVisible({ timeout: 8000 });
    // Score badge must not appear (surface-aware rule: no fabricated score)
    await expect(page.getByText(/^Score \d+$/)).not.toBeVisible();
  });

  test('Contents navigate: picking a section brings it into view', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');
    await openDock(page, 'toc-dock-toggle');

    const nav = page.getByRole('navigation', { name: 'Paper navigation' });
    await expect(nav).toBeVisible({ timeout: 8000 });

    await nav.getByRole('button', { name: /Your Notes/ }).click();

    await expect(page.locator('#section-notes')).toBeInViewport({ timeout: 8000 });
  });
});

// ── Narrow-viewport sheet tests ────────────────────────────────────────────

test.describe('Paper Detail reading layout — narrow viewport', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
  });

  test('the panels are offered as sheets, not docks', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Use heading role to avoid strict-mode: title appears in both h1 and breadcrumb span.
    await expect(page.getByRole('heading', { name: 'Attention Is All You Need' })).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('toc-sheet-trigger')).toBeVisible();
    await expect(page.getByTestId('actions-sheet-trigger')).toBeVisible();
    await expect(page.getByTestId('toc-dock-toggle')).toBeHidden();
    await expect(page.getByTestId('actions-dock-toggle')).toBeHidden();
  });

  test('opening the Contents sheet shows the sections, and picking one closes it', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await page.getByTestId('toc-sheet-trigger').click();

    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText('Brief')).toBeVisible({ timeout: 5000 });
    await expect(sheet.getByText('Methodology')).toBeVisible();

    await sheet.getByRole('button', { name: /Your Notes/ }).click();

    // The sheet gets out of the way first, then the section arrives.
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 5000 });
    await expect(page.locator('#section-notes')).toBeInViewport({ timeout: 8000 });
  });

  test('opening the Actions sheet shows the pipeline status', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await page.getByTestId('actions-sheet-trigger').click();

    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText(/Analyzed — passages extracted/)).toBeVisible({
      timeout: 5000,
    });
  });
});

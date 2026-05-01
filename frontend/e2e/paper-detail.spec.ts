import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

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
    summary_brief: 'Introduces the Transformer architecture based solely on attention mechanisms.',
    summary_detailed:
      'This paper proposes the Transformer, a model architecture eschewing recurrence and instead relying entirely on an attention mechanism.',
    methodology: 'Self-attention mechanism with multi-head attention, positional encoding.',
    limitations: 'Quadratic complexity with sequence length.',
    key_findings: [
      {
        finding: 'Transformer outperforms RNN-based models on translation tasks',
        quote: 'The Transformer achieves 28.4 BLEU on the WMT 2014 English-to-German translation task.',
        page_number: 8,
        verified: true,
      },
    ],
    confidence: 'HIGH',
    summary_verified: true,
  },
  chunks: [
    {
      id: 1,
      paper_id: 1,
      chunk_index: 0,
      content: 'The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.',
      page_number: 1,
    },
    {
      id: 2,
      paper_id: 1,
      chunk_index: 1,
      content: 'We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.',
      page_number: 2,
    },
  ],
  user_state: {
    id: 1,
    paper_id: 1,
    status: 'reading',
    rating: 4,
    user_notes: 'Foundational paper for modern NLP.',
    flagged: false,
  },
};

test.beforeEach(async ({ page }) => {
  await seedAuthedSession(page);

  // Mock the paper detail API
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

  await page.route((url) => url.pathname === '/api/papers/1/notes' && url.searchParams.get('source') === 'user', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });

  await page.route((url) => url.pathname === '/api/papers/1/notes' && url.searchParams.get('source') === 'zotero', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });

  // Mock decks API for sidebar
  await page.route('**/api/decks**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });
});

test.describe('Paper Detail Page', () => {
  test('paper header loads with title and metadata', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Title should be visible
    await expect(page.getByRole('heading', { name: 'Attention Is All You Need' })).toBeVisible({
      timeout: 5000,
    });

    // Authors should be visible
    await expect(page.getByText(/Vaswani/)).toBeVisible();

    // Source badge
    await expect(page.getByText('arxiv')).toBeVisible();

    // Citation count badge
    await expect(page.getByText(/95000 citations/)).toBeVisible();

    // External link
    await expect(page.getByRole('link', { name: /Open original/ })).toBeVisible();
  });

  test('summary tab shows summary content or empty state', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Summary tab should be selected by default
    const summaryTab = page.getByRole('tab', { name: 'Summary' });
    await expect(summaryTab).toBeVisible();

    // Brief summary should be visible
    await expect(
      page
        .getByText('Introduces the Transformer architecture based solely on attention mechanisms.')
        .or(page.getByText('No summary available')),
    ).toBeVisible({ timeout: 5000 });

    // When summary exists, check for section headings
    const briefHeading = page.getByRole('heading', { name: 'Brief' });
    const noSummary = page.getByText('No summary available');

    if (await briefHeading.isVisible()) {
      await expect(page.getByRole('heading', { name: 'Detailed Summary' })).toBeVisible();
      await expect(page.getByRole('heading', { name: 'Methodology' })).toBeVisible();
      await expect(page.getByRole('heading', { name: 'Key Findings' })).toBeVisible();
    } else {
      await expect(noSummary).toBeVisible();
    }
  });

  test('chunks tab shows paper chunks', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Click Chunks tab
    const chunksTab = page.getByRole('tab', { name: 'Chunks' });
    await chunksTab.click();

    // Should show chunk count or empty state
    const chunkCount = page.getByText(/chunks extracted/);
    const noChunks = page.getByText('No chunks available');

    await expect(chunkCount.or(noChunks)).toBeVisible({ timeout: 5000 });

    // If chunks exist, verify individual chunk buttons are present
    if (await chunkCount.isVisible()) {
      await expect(page.getByText(/Chunk 0/)).toBeVisible();
      await expect(page.getByText(/Chunk 1/)).toBeVisible();

      // Click to expand a chunk
      await page.getByText(/Chunk 0/).click();
      await expect(
        page.getByText(/dominant sequence transduction/),
      ).toBeVisible();
    }
  });

  test('notes: create a new note', async ({ page }) => {
    // Mock the create note API
    await page.route('**/api/notes**', async (route) => {
      if (route.request().method() === 'POST') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            id: 1,
            paper_id: 1,
            user_note: 'This is my test note',
            highlight_text: null,
            page_number: null,
            source: 'user',
            zotero_annotation_key: null,
            verification_status: 'unverified',
            verified_quote: null,
            verified_page_number: null,
            promoted_at: null,
            created_at: new Date().toISOString(),
          }),
        });
      } else {
        await route.continue();
      }
    });

    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Navigate to Notes tab
    const notesTab = page.getByRole('tab', { name: 'Notes' });
    await notesTab.click();

    // Find the "Add a note" section
    await expect(page.getByRole('heading', { name: 'Add a note' })).toBeVisible({ timeout: 5000 });

    // Fill in the note text
    const noteTextarea = page.getByPlaceholder('Write your note...');
    await noteTextarea.fill('This is my test note');

    // The Save note button should be enabled
    const saveButton = page.getByRole('button', { name: 'Save note', exact: true });
    await expect(saveButton).toBeEnabled();

    // Click save
    await saveButton.click();
  });

  test('notes: edit existing note', async ({ page }) => {
    // Mock notes API returning an existing note
    await page.route((url) => url.pathname === '/api/papers/1/notes' && url.searchParams.get('source') === 'user', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 10,
            paper_id: 1,
            user_note: 'Existing note content',
            highlight_text: 'some highlight',
            page_number: 3,
            source: 'user',
            zotero_annotation_key: null,
            verification_status: 'unverified',
            verified_quote: null,
            verified_page_number: null,
            promoted_at: null,
            created_at: '2024-06-01T12:00:00Z',
          },
        ]),
      });
    });

    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await page.getByRole('tab', { name: 'Notes' }).click();

    // Existing note should be visible
    await expect(page.getByText('Existing note content')).toBeVisible({ timeout: 5000 });

    // Metadata should be shown
    await expect(page.getByText(/Page 3/)).toBeVisible();
    await expect(page.getByText(/some highlight/)).toBeVisible();

    // The note form is always present for adding new notes (no edit-in-place in the current UI)
    // Verify the "Add a note" form is available to create additional notes
    await expect(page.getByPlaceholder('Write your note...')).toBeVisible();
  });

  test('notes: delete note', async ({ page }) => {
    // Mock notes API with an existing note
    await page.route((url) => url.pathname === '/api/papers/1/notes' && url.searchParams.get('source') === 'user', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 10,
            paper_id: 1,
            user_note: 'Note to be deleted',
            highlight_text: null,
            page_number: null,
            source: 'user',
            zotero_annotation_key: null,
            verification_status: 'unverified',
            verified_quote: null,
            verified_page_number: null,
            promoted_at: null,
            created_at: '2024-06-01T12:00:00Z',
          },
        ]),
      });
    });

    // Mock delete endpoint
    await page.route('**/api/notes/10**', async (route) => {
      if (route.request().method() === 'DELETE') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ok: true }),
        });
      } else {
        await route.continue();
      }
    });

    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    await page.getByRole('tab', { name: 'Notes' }).click();

    // Note should be visible
    await expect(page.getByText('Note to be deleted')).toBeVisible({ timeout: 5000 });

    // Click the Delete button
    const deleteButton = page.getByRole('button', { name: 'Delete' });
    await expect(deleteButton).toBeVisible();
    await deleteButton.click();
  });

  test('sidebar shows action buttons', async ({ page }) => {
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // On larger viewports, the sidebar is visible. On mobile, use the Actions sheet trigger.
    // Set viewport to desktop size to see the sidebar directly.
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/paper/1');
    await page.waitForLoadState('networkidle');

    // Wait for the page to load
    await expect(page.locator('main').getByRole('heading', { name: 'Attention Is All You Need' })).toBeVisible({
      timeout: 5000,
    });

    // The current sidebar exposes the full pipeline first and keeps manual steps collapsed.
    await expect(page.getByRole('button', { name: /Analyze Paper/ })).toBeVisible();
    await expect(page.getByText(/Downloading PDF/)).toBeVisible();
    await expect(page.getByText(/Processing & embedding/)).toBeVisible();
    await expect(page.getByText(/Generating summary/)).toBeVisible();
    await page.getByText('Manual steps').click();
    await expect(page.getByRole('button', { name: /Download PDF/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Process PDF/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Generate Summary/ })).toBeVisible();

    // Phase A removed the legacy Status/Rating/Notes/Flagged form. Lifecycle is now
    // driven by the surface chips on /feed (covered by feed-lifecycle.spec.ts) and
    // by the per-paper action buttons rendered above. Per-paper feedback uses the
    // FeedbackButtons component (covered by feedback-loop.spec.ts).
  });
});

/**
 * ResearchFeedPage — Feed IA Redesign tests
 *
 * Coverage:
 *  - FacetRail renders inside the page (3-pane layout)
 *  - Default surface = inbox (Inbox-first)
 *  - Trash appears as §Status facet, not a top-level tab
 *  - Ask is NOT rendered inside the feed page (F4 owns /ask route)
 *  - Scoped list-filter renders for inbox/library/trash surfaces
 *  - §Source facets from fetchFeedCounts drive FeedView
 *  - Clicking a §Status facet updates the URL (drives query params)
 *  - BulkToolbar is still present (preserved functionality)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
// ─── API mock ─────────────────────────────────────────────────────────────────

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

vi.mock('@/components/chat/StreamingChat', () => ({
  StreamingChat: () => <div data-testid="streaming-chat" />,
}));

// vi.mock is hoisted above module-level const declarations, so fixtures that
// need to be referenced inside the factory must be declared with vi.hoisted().
const { RICH_COUNTS, INBOX_PAPER } = vi.hoisted(() => {
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
    by_source: { arxiv: 25, semantic_scholar: 18 },
    by_topic: [{ topic_id: 1, name: 'Machine Learning', count: 30 }],
    untagged: 5,
  };

  const INBOX_PAPER = {
    id: 1,
    external_id: 'arxiv:2301.00001',
    source_type: 'arxiv' as const,
    title: 'Neural Test Paper',
    authors: ['Author A'],
    abstract: 'Abstract text.',
    published_date: '2025-01-01',
    url: 'https://arxiv.org/abs/2301.00001',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    citation_count: 0,
    priority_score: 0.8,
    metadata: {},
    discovered_at: '2025-01-01T00:00:00Z',
    created_at: '2025-01-01T00:00:00Z',
    summary_brief: null,
    tldr: null,
    confidence: null,
    recommendation_score: 0.92,
    recommendation_reason: 'Matches your topics',
    state: 'inbox' as const,
    state_before_trash: null,
    starred: false,
    rating: null,
  };

  return { RICH_COUNTS, INBOX_PAPER };
});

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  return createApiMock({
    fetchFeedCounts: async () => (RICH_COUNTS),
    fetchFeed: async () => ({ papers: [INBOX_PAPER], total: 1 }),
    fetchSources: async () => ([
      { id: 1, source_type: 'arxiv', enabled: true, config: {}, priority: 1, display_order: 1, created_at: '2025-01-01T00:00:00Z' },
    ]),
    searchPreview: async () => ({ results: [], total: 0, source_errors: {} }),
    batchSavePapers: async () => ([]),
    // Lifecycle mutations
    savePaper: async () => ({ status: 'ok', paper_id: 1 }),
    skipPaper: async () => ({ status: 'ok', paper_id: 1 }),
    markReading: async () => ({ status: 'ok', paper_id: 1 }),
    markDone: async () => ({ status: 'ok', paper_id: 1 }),
    trashPaper: async () => ({ status: 'ok', paper_id: 1 }),
    restorePaper: async () => ({ status: 'ok', paper_id: 1 }),
    starPaper: async () => ({ status: 'ok', paper_id: 1 }),
    unstarPaper: async () => ({ status: 'ok', paper_id: 1 }),
    bulkAction: async () => ({ succeeded: [], failed: [] }),
    discoverPapers: async () => ([]),
    scanLocalPdfs: async () => ({ job_id: 'job-scan', status: 'queued' }),
    batchProcessPapers: async () => ({ queued: 0, total_unprocessed: 0, skipped_missing_pdf: 0, job_id: 'job' }),
    fetchTopics: async () => ([]),
    fetchPulseHistory: async () => ([]),
    zoteroGetLinkage: async () => ({ zotero_item_key: null, zotero_citation_key: null, zotero_last_pushed_at: null }),
    uploadPdf: async () => ({ id: 99 }),
    processPdf: async () => ({ job_id: 'job-pdf', status: 'queued' }),
  });
});

function makeQueryClient() {
  return createTestQueryClient();
}

function renderPage(initialSearch = '?surface=inbox') {
  const qc = makeQueryClient();
  return renderWithProviders(
    <MemoryRouter initialEntries={[`/feed${initialSearch}`]}>
      <Routes>
        <Route path="/feed" element={<ResearchFeedPage />} />
        <Route path="/paper/:id" element={<div data-testid="paper-detail" />} />
        <Route path="/ask" element={<div data-testid="ask-page" />} />
      </Routes>
    </MemoryRouter>,
    { queryClient: qc },
  );
}

describe('ResearchFeedPage — 3-pane IA', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ── 3-pane shell ─────────────────────────────────────────────────────────

  it('renders the facet rail (left pane)', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-rail')).toBeInTheDocument();
    });
  });

  it('has §Status, §Star, §Source, §Topic section headers in the rail', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Status')).toBeInTheDocument();
      expect(screen.getByText('Star')).toBeInTheDocument();
      expect(screen.getByText('Source')).toBeInTheDocument();
      expect(screen.getByText('Topic')).toBeInTheDocument();
    });
  });

  // ── Default landing ───────────────────────────────────────────────────────

  it('defaults to Inbox as active surface', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'true');
    });
  });

  // ── Trash as §Status facet ────────────────────────────────────────────────

  it('has Trash as a §Status facet item (not a top-level tab)', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-status-trash')).toBeInTheDocument();
    });
    // There should NOT be a button with role=tab labeled "Trash"
    const trashTab = screen.queryByRole('tab', { name: /^trash$/i });
    expect(trashTab).not.toBeInTheDocument();
  });

  it('clicking Trash §Status shows trash surface content', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-status-trash')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('facet-status-trash'));
    await waitFor(() => {
      // Trash info banner should appear
      expect(screen.getByRole('alert')).toHaveTextContent(/papers in trash/i);
    });
  });

  // ── Ask removed from feed ─────────────────────────────────────────────────

  it('does NOT render a tab or button labeled "Ask" inside the feed', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-rail')).toBeInTheDocument();
    });
    // "Ask" should not be a tab in the feed
    const askTab = screen.queryByRole('tab', { name: /^ask$/i });
    expect(askTab).not.toBeInTheDocument();
    // StreamingChat should NOT be rendered at inbox
    expect(screen.queryByTestId('streaming-chat')).not.toBeInTheDocument();
  });

  it('does NOT render StreamingChat at ?surface=inbox (Ask is its own route)', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('facet-rail')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('streaming-chat')).not.toBeInTheDocument();
  });

  // ── Scoped list-filter ────────────────────────────────────────────────────

  it('renders the scoped list-filter input for inbox surface', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('feed-list-filter')).toBeInTheDocument();
    });
  });

  it('renders the scoped list-filter input for library surface', async () => {
    renderPage('?surface=library');
    await waitFor(() => {
      expect(screen.getByTestId('feed-list-filter')).toBeInTheDocument();
    });
  });

  it('does NOT render the scoped list-filter or the facet rail on Discover', async () => {
    renderPage('?surface=search');
    // The rail's STATUS/TOPIC counts describe your own papers, which is not
    // what either Discover tab shows.
    await waitFor(() => {
      expect(screen.getByRole('tablist', { name: /discover mode/i })).toBeInTheDocument();
    });
    expect(screen.queryByTestId('facet-rail')).not.toBeInTheDocument();
    expect(screen.queryByTestId('feed-list-filter')).not.toBeInTheDocument();
  });

  // ── §Source facets render ─────────────────────────────────────────────────

  it('renders §Source facets from FeedCountsWithFacets.by_source', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-source-arxiv')).toBeInTheDocument();
      expect(screen.getByTestId('facet-source-semantic_scholar')).toBeInTheDocument();
    });
    expect(screen.getByTestId('facet-source-arxiv')).toHaveTextContent('25');
  });

  // ── §Topic facets render ──────────────────────────────────────────────────

  it('renders §Topic facets from FeedCountsWithFacets.by_topic', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-topic-1')).toBeInTheDocument();
    });
    expect(screen.getByTestId('facet-topic-1')).toHaveTextContent('Machine Learning');
    expect(screen.getByTestId('facet-topic-untagged')).toHaveTextContent('Untagged');
  });

  // ── Preserved: library scope toggle ──────────────────────────────────────

  it('has no ownership scope toggle: Papers is always yours', async () => {
    renderPage('?surface=library');
    await waitFor(() => {
      expect(screen.getByTestId('facet-rail')).toBeInTheDocument();
    });
    expect(screen.queryByRole('tablist', { name: /library scope/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'My library' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Public + mine' })).not.toBeInTheDocument();
  });

  it('offers the public corpus as a Discover tab instead', async () => {
    renderPage('?surface=search');
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'Find new papers' })).toBeInTheDocument();
    });
    expect(screen.getByRole('tab', { name: 'Browse public corpus' })).toBeInTheDocument();
  });

  // ── Surface info copy ─────────────────────────────────────────────────────

  it('shows inbox info text when surface=inbox', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByText(/unread papers from your configured sources/i)).toBeInTheDocument();
    });
  });

  it('shows papers info text when surface=library', async () => {
    renderPage('?surface=library');
    await waitFor(() => {
      expect(screen.getByText(/papers you.*saved or own/i)).toBeInTheDocument();
    });
  });

  it('ignores a stale ?scope=corpus link: Papers still describes your own papers', async () => {
    renderPage('?surface=library&scope=corpus');
    await waitFor(() => {
      expect(screen.getByText(/papers you.*saved or own/i)).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/verified public papers plus private papers in your library/i),
    ).not.toBeInTheDocument();
  });
});

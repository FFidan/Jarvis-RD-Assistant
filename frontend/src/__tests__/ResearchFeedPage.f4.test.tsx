/**
 * ResearchFeedPage — F4 UX improvements
 *
 * Coverage:
 *  - Per-filter subtitles differ on library surface (all / reading / to_read / done)
 *  - FacetRail shows honest empty-source copy (library-scoped explanation)
 *  - FacetRail shows honest empty-topic copy (library-scoped explanation)
 *  - Upload PDF button is present on Inbox surface
 *  - Upload PDF button is present on Library surface
 *  - Upload PDF button navigates to search surface
 *  - Upload PDF button is NOT rendered on Trash surface
 *  - Filter placeholder not clipped (wrapper uses flex-1 layout)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('@/components/chat/StreamingChat', () => ({
  StreamingChat: () => <div data-testid="streaming-chat" />,
}));

// vi.mock is hoisted above module-level const declarations.  Fixtures that need
// to be referenced inside the factory MUST be declared with vi.hoisted().
const { RICH_COUNTS, EMPTY_COUNTS, INBOX_PAPER } = vi.hoisted(() => {
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

  const EMPTY_COUNTS = {
    inbox: 0,
    library: 0,
    reading_list: 0,
    reading: 0,
    done: 0,
    starred: 0,
    trash: 0,
    active: 0,
    kept: 0,
    all_non_trash: 0,
    by_source: {},
    by_topic: [],
    untagged: 0,
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

  return { RICH_COUNTS, EMPTY_COUNTS, INBOX_PAPER };
});

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchFeedCounts: vi.fn().mockResolvedValue(RICH_COUNTS),
    fetchFeedCountsWithFacets: vi.fn().mockResolvedValue(RICH_COUNTS),
    fetchFeed: vi.fn().mockResolvedValue({ papers: [INBOX_PAPER], total: 1 }),
    fetchSources: vi.fn().mockResolvedValue([
      { id: 1, source_type: 'arxiv', enabled: true, config: {}, priority: 1, display_order: 1, created_at: '2025-01-01T00:00:00Z' },
    ]),
    searchPreview: vi.fn().mockResolvedValue({ results: [], total: 0, source_errors: {} }),
    batchSavePapers: vi.fn().mockResolvedValue([]),
    savePaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    skipPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    markReading: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    markDone: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    trashPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    restorePaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    starPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    unstarPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    bulkAction: vi.fn().mockResolvedValue({ succeeded: [], failed: [] }),
    discoverPapers: vi.fn().mockResolvedValue([]),
    scanLocalPdfs: vi.fn().mockResolvedValue({ job_id: 'job-scan', status: 'queued' }),
    batchProcessPapers: vi.fn().mockResolvedValue({ queued: 0, total_unprocessed: 0, skipped_missing_pdf: 0, job_id: 'job' }),
    fetchTopics: vi.fn().mockResolvedValue([]),
    fetchPulseHistory: vi.fn().mockResolvedValue([]),
    zoteroGetLinkage: vi.fn().mockResolvedValue({ zotero_item_key: null, zotero_citation_key: null, zotero_last_pushed_at: null }),
    uploadPdf: vi.fn().mockResolvedValue({ id: 99 }),
    processPdf: vi.fn().mockResolvedValue({ job_id: 'job-pdf', status: 'queued' }),
  };
});

vi.mock('@/lib/query-persister', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/query-persister')>();
  return {
    ...actual,
    getPersistedCacheTimestamp: vi.fn().mockResolvedValue(null),
    attachQueryPersister: vi.fn().mockReturnValue(() => {}),
    clearPersistedQueryCache: vi.fn().mockResolvedValue(undefined),
    flushPersistedQueryCache: vi.fn().mockResolvedValue(undefined),
  };
});

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderPage(initialSearch = '?surface=inbox') {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/feed${initialSearch}`]}>
        <Routes>
          <Route path="/feed" element={<ResearchFeedPage />} />
          <Route path="/paper/:id" element={<div data-testid="paper-detail" />} />
          <Route path="/ask" element={<div data-testid="ask-page" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ResearchFeedPage — F4 per-filter subtitles', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows generic library subtitle when no filter is active', async () => {
    renderPage('?surface=library');
    await waitFor(() => {
      // C-FEED: copy updated to "My library — papers you've saved or own."
      expect(
        screen.getByText(/my library.*saved.*own/i),
      ).toBeInTheDocument();
    });
  });

  it('shows "currently reading" subtitle for filter=reading', async () => {
    renderPage('?surface=library&filter=reading');
    await waitFor(() => {
      expect(screen.getByText(/papers you're currently reading/i)).toBeInTheDocument();
    });
  });

  it('shows "saved to read later" subtitle for filter=to_read', async () => {
    renderPage('?surface=library&filter=to_read');
    await waitFor(() => {
      expect(screen.getByText(/papers saved to read later/i)).toBeInTheDocument();
    });
  });

  it('shows "finished" subtitle for filter=done', async () => {
    renderPage('?surface=library&filter=done');
    await waitFor(() => {
      expect(screen.getByText(/papers you've finished/i)).toBeInTheDocument();
    });
  });

  it('shows inbox subtitle unchanged on inbox surface', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByText(/unread papers from your configured sources/i)).toBeInTheDocument();
    });
  });
});

describe('ResearchFeedPage — F4 discoverable upload', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders Upload PDF button on Inbox surface', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('upload-pdf-button')).toBeInTheDocument();
    });
    expect(screen.getByTestId('upload-pdf-button')).toHaveTextContent('Upload PDF');
  });

  it('renders Upload PDF button on Library surface', async () => {
    renderPage('?surface=library');
    await waitFor(() => {
      expect(screen.getByTestId('upload-pdf-button')).toBeInTheDocument();
    });
    expect(screen.getByTestId('upload-pdf-button')).toHaveTextContent('Upload PDF');
  });

  it('clicking Upload PDF on Inbox navigates to search surface and hoists the upload zone', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('upload-pdf-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('upload-pdf-button'));
    // After click, search surface content and the hoisted upload zone appear.
    await waitFor(() => {
      expect(screen.getByText(/search external databases/i)).toBeInTheDocument();
    });
    expect(screen.getByTestId('upload-zone-hoisted')).toBeInTheDocument();
  });

  it('does NOT render Upload PDF button on Trash surface', async () => {
    renderPage('?surface=trash');
    await waitFor(() => {
      // Trash alert should be visible (confirming trash surface rendered)
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('upload-pdf-button')).not.toBeInTheDocument();
  });
});

describe('ResearchFeedPage — surface-aware H1', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows "Library" H1 on library surface', async () => {
    renderPage('?surface=library');
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Library');
    });
  });

  it('shows "Discover" H1 on search surface', async () => {
    renderPage('?surface=search');
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Discover');
    });
  });

  it('shows "Library" H1 on inbox surface (not search)', async () => {
    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Library');
    });
  });
});

describe('ResearchFeedPage — Untagged facet end-to-end', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('threads facet_topic=untagged through FeedView to fetchFeed as untagged=true', async () => {
    const { fetchFeed } = await import('@/lib/api');
    renderPage('?surface=inbox&facet_topic=untagged');

    await waitFor(() =>
      expect(vi.mocked(fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ untagged: true }),
      ),
    );
  });

  it('does not set untagged when a numeric topic facet is active', async () => {
    const { fetchFeed } = await import('@/lib/api');
    renderPage('?surface=inbox&facet_topic=1');

    await waitFor(() => expect(vi.mocked(fetchFeed)).toHaveBeenCalled());
    for (const [params] of vi.mocked(fetchFeed).mock.calls) {
      expect((params as { untagged?: boolean }).untagged).toBeFalsy();
    }
  });
});

describe('FacetRail — F4 honest facet empty-state copy', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows library-scoped empty-source message when no papers exist', async () => {
    // Force empty counts so the empty-state branch fires
    const { fetchFeedCountsWithFacets, fetchFeedCounts } =
      await import('@/lib/api');
    (fetchFeedCountsWithFacets as ReturnType<typeof vi.fn>).mockResolvedValue(EMPTY_COUNTS);
    (fetchFeedCounts as ReturnType<typeof vi.fn>).mockResolvedValue(EMPTY_COUNTS);

    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('facet-source-empty')).toBeInTheDocument();
    });
    expect(screen.getByTestId('facet-source-empty')).toHaveTextContent(
      'No papers in your library yet',
    );
  });

  it('shows library-scoped empty-topic message when no topics configured', async () => {
    const { fetchFeedCountsWithFacets, fetchFeedCounts } =
      await import('@/lib/api');
    (fetchFeedCountsWithFacets as ReturnType<typeof vi.fn>).mockResolvedValue(EMPTY_COUNTS);
    (fetchFeedCounts as ReturnType<typeof vi.fn>).mockResolvedValue(EMPTY_COUNTS);

    renderPage('?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('facet-topic-empty')).toBeInTheDocument();
    });
    expect(screen.getByTestId('facet-topic-empty')).toHaveTextContent(
      'No library papers tagged with a topic yet',
    );
  });
});

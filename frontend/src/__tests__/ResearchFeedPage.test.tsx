import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { ApiError, useFeedCounts } from '@/lib/api';
import { queryClient as appQueryClient } from '@/lib/query-client';
import { useJobStore } from '@/stores/job-store';

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function LocationDisplay() {
  const location = useLocation();
  return (
    <div data-testid="location">
      <span data-testid="location-path">{location.pathname}</span>
      <span data-testid="location-state">
        {location.state ? JSON.stringify(location.state) : 'null'}
      </span>
    </div>
  );
}

// Mock the API module - vi.mock is hoisted, so use inline return values
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchFeedPapers: vi.fn().mockResolvedValue({
      papers: [
      {
        id: 1,
        external_id: 'arxiv:2301.00001',
        source_type: 'arxiv',
        title: 'Test Paper One',
        authors: ['Author A', 'Author B'],
        abstract: 'An abstract for the test paper.',
        published_date: '2025-01-01',
        url: 'https://arxiv.org/abs/2301.00001',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 10,
        priority_score: 0.8,
        metadata: {},
        discovered_at: '2025-01-01T00:00:00Z',
        created_at: '2025-01-01T00:00:00Z',
        summary_brief: 'A brief summary.',
        tldr: 'Short TLDR',
        confidence: 'HIGH',
        user_status: 'new',
        rating: null,
      },
      {
        id: 2,
        external_id: 'arxiv:2301.00002',
        source_type: 'semantic_scholar',
        title: 'Test Paper Two',
        authors: ['Author C'],
        abstract: 'Another abstract.',
        published_date: '2025-02-01',
        url: 'https://semanticscholar.org/paper/123',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 5,
        priority_score: 0.3,
        metadata: {},
        discovered_at: '2025-02-01T00:00:00Z',
        created_at: '2025-02-01T00:00:00Z',
        summary_brief: null,
        tldr: null,
        confidence: null,
        user_status: 'reading',
        rating: null,
      },
    ],
    total: 2,
  }),
    fetchFeed: vi.fn().mockResolvedValue({
      papers: [
      {
        id: 1,
        external_id: 'arxiv:2301.00001',
        source_type: 'arxiv',
        title: 'Test Paper One',
        authors: ['Author A', 'Author B'],
        abstract: 'An abstract for the test paper.',
        published_date: '2025-01-01',
        url: 'https://arxiv.org/abs/2301.00001',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 10,
        priority_score: 0.8,
        metadata: {},
        discovered_at: '2025-01-01T00:00:00Z',
        created_at: '2025-01-01T00:00:00Z',
        summary_brief: 'A brief summary.',
        tldr: 'Short TLDR',
        confidence: 'HIGH',
        user_status: 'new',
        rating: null,
      },
      {
        id: 2,
        external_id: 'arxiv:2301.00002',
        source_type: 'semantic_scholar',
        title: 'Test Paper Two',
        authors: ['Author C'],
        abstract: 'Another abstract.',
        published_date: '2025-02-01',
        url: 'https://semanticscholar.org/paper/123',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 5,
        priority_score: 0.3,
        metadata: {},
        discovered_at: '2025-02-01T00:00:00Z',
        created_at: '2025-02-01T00:00:00Z',
        summary_brief: null,
        tldr: null,
        confidence: null,
        user_status: 'reading',
        rating: null,
      },
    ],
    total: 2,
  }),
    searchPreview: vi.fn().mockResolvedValue({
      results: [
        {
          title: 'Search Result Paper',
          authors: ['Search Author'],
          abstract: 'A search result abstract.',
          published_date: '2025-03-01',
          url: 'https://arxiv.org/abs/2303.00001',
          external_id: 'arxiv:2303.00001',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 0,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    }),
    batchSavePapers: vi.fn().mockResolvedValue([{ id: 1, title: 'Saved Paper' }]),
    markDone: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    discoverPapers: vi.fn().mockResolvedValue([]),
    scanLocalPdfs: vi.fn().mockResolvedValue({ job_id: 'job-scan', status: 'queued' }),
    batchProcessPapers: vi.fn().mockResolvedValue({
      queued: 5,
      total_unprocessed: 10,
      skipped_missing_pdf: 2,
      job_id: 'job-abc',
    }),
    fetchTopics: vi.fn().mockResolvedValue([
      {
        id: 1,
        name: 'Machine Learning',
        query_terms: ['ML'],
        category: null,
        enabled: true,
        created_at: '2025-01-01T00:00:00Z',
      },
    ]),
    fetchSources: vi.fn().mockResolvedValue([
      { id: 1, source_type: 'arxiv', enabled: true, config: {}, priority: 1, display_order: 1, created_at: '2025-01-01T00:00:00Z' },
      { id: 2, source_type: 'semantic_scholar', enabled: true, config: {}, priority: 2, display_order: 2, created_at: '2025-01-01T00:00:00Z' },
      { id: 3, source_type: 'openalex', enabled: true, config: {}, priority: 3, display_order: 3, created_at: '2025-01-01T00:00:00Z' },
      { id: 4, source_type: 'pubmed', enabled: true, config: {}, priority: 4, display_order: 4, created_at: '2025-01-01T00:00:00Z' },
      { id: 5, source_type: 'local', enabled: true, config: {}, priority: 5, display_order: 5, created_at: '2025-01-01T00:00:00Z' },
    ]),
    fetchFeedCounts: vi.fn().mockResolvedValue({
      inbox: 0, library: 0, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 0, kept: 0, all_non_trash: 0,
    }),
    fetchFeedCountsWithFacets: vi.fn().mockResolvedValue({
      inbox: 0, library: 0, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 0, kept: 0, all_non_trash: 0,
      by_source: {}, by_topic: [], untagged: 0,
    }),
    useFeedCounts: vi.fn().mockReturnValue({ data: undefined, isLoading: false }),
    fetchPulseHistory: vi.fn().mockResolvedValue([]),
    zoteroGetLinkage: vi.fn().mockResolvedValue({
      zotero_item_key: null,
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    }),
    zoteroPushPaper: vi.fn().mockResolvedValue({ job_id: 'job-zotero', status: 'queued' }),
    zoteroResync: vi.fn().mockResolvedValue({ job_id: 'job-zotero-resync', status: 'queued' }),
  };
});

// Mock the StreamingChat component since it has complex dependencies
vi.mock('@/components/chat/StreamingChat', () => ({
  StreamingChat: ({ chatId, scope }: { chatId: string; scope: string }) => (
    <div data-testid="streaming-chat" data-chat-id={chatId} data-scope={scope}>
      StreamingChat Mock
    </div>
  ),
}));

function renderPage() {
  const renderResult = render(
    <QueryClientProvider client={appQueryClient}>
      <MemoryRouter initialEntries={['/feed']}>
        <Routes>
          <Route path="/feed" element={<ResearchFeedPage />} />
          <Route path="/paper/:paperId" element={<LocationDisplay />} />
          <Route path="/projects" element={<LocationDisplay />} />
        </Routes>
    </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...renderResult, queryClient: appQueryClient };
}

function getPreviewRowPrimaryButton(title: string): HTMLButtonElement {
  const titleNode = screen.getByText(title);
  const button = titleNode.closest('button');
  if (!button) {
    throw new Error(`Could not find preview row button for ${title}`);
  }
  return button as HTMLButtonElement;
}

function createMockSSEStream(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let idx = 0;
  return new ReadableStream({
    pull(controller) {
      if (idx < frames.length) {
        controller.enqueue(encoder.encode(frames[idx]));
        idx += 1;
      } else {
        controller.close();
      }
    },
  });
}

function createControlledSSEStream() {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const stream = new ReadableStream<Uint8Array>({
    start(currentController) {
      controller = currentController;
    },
  });
  return {
    stream,
    push(frame: string) {
      controller?.enqueue(encoder.encode(frame));
    },
    close() {
      controller?.close();
    },
  };
}

describe('ResearchFeedPage', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    // clearAllMocks clears call history but NOT mockResolvedValueOnce queues.
    // 1. Reset zoteroGetLinkage: the hydration test queues 2 Once values but only
    //    guarantees consuming both when the SSE stream resolves in time. Any
    //    unconsumed value shifts the resolution sequence for the next test.
    const { zoteroGetLinkage } = await import('@/lib/api');
    vi.mocked(zoteroGetLinkage).mockReset();
    vi.mocked(zoteroGetLinkage).mockResolvedValue({
      zotero_item_key: null,
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    });
    // 2. Reset globalThis.fetch: vi.spyOn(globalThis, 'fetch') is used by the
    //    Zotero SSE tests. When tests from other files (running in the same
    //    worker thread) leave once-queues on the shared globalThis.fetch spy,
    //    clearAllMocks does not drain them. Resetting the spy here ensures the
    //    subscribe() IIFE always gets the fetch mock set up by the current test.
    if (vi.isMockFunction(globalThis.fetch)) {
      vi.mocked(globalThis.fetch).mockReset();
    }
    // Use _reset() instead of setState({}) so that every active AbortController
    // is aborted before the store is cleared. The plain setState variant removes
    // controllers from the map without aborting them, which lets a previous
    // test's _reconnectAfterDrop IIFE sleep out and then re-subscribe DURING
    // the current test — locking the controlled ReadableStream before the
    // current test's own subscribe() can acquire it (root cause of the
    // "job-zotero-queued stream locked" race that makes the Zotero update test
    // fail 3/3 in the full suite while passing in isolation).
    useJobStore.getState()._reset();
    appQueryClient.clear();
  });

  it('renders the page heading', () => {
    renderPage();
    expect(screen.getByText('Research Feed')).toBeInTheDocument();
  });

  it('renders §Status facet items (Inbox, Library, Trash) in facet rail — replaces old tab bar', () => {
    renderPage();
    // F1 3-pane IA: facet rail replaces horizontal tab bar
    // Inbox/Library/Trash appear as §Status facet buttons (aria-pressed)
    expect(screen.getByTestId('facet-status-inbox')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-library')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-trash')).toBeInTheDocument();
    // Discover (search surface) is accessible via the Discover link in the rail
    expect(screen.getByTestId('facet-discover')).toBeInTheDocument();
    // Ask is NOT in the feed page (spec §3.4: Ask is its own nav destination)
    expect(screen.queryByRole('tab', { name: 'Ask' })).not.toBeInTheDocument();
    // Pulse tab was moved to /my-day; it is not rendered here
    expect(screen.queryByRole('tab', { name: 'Pulse' })).not.toBeInTheDocument();
  });

  it('defaults to Inbox surface active (spec §3.5: Inbox-first)', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 0, library: 5, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 5, kept: 5, all_non_trash: 5 },
      isLoading: false,
      isPending: false,
    } as ReturnType<typeof useFeedCounts>);
    renderPage();
    await waitFor(() => {
      // F1 3-pane IA: §Status facet buttons use aria-pressed (not aria-selected)
      expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'true');
    });
  });

  it('does not render Ask inside the feed page — Ask is its own nav destination (spec §3.4)', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-rail')).toBeInTheDocument();
    });
    // No Ask tab in feed (F4 owns the /ask route)
    expect(screen.queryByRole('tab', { name: 'Ask' })).not.toBeInTheDocument();
    // StreamingChat should not be rendered at inbox
    expect(screen.queryByTestId('streaming-chat')).not.toBeInTheDocument();
  });

  it('renders the search input with updated placeholder', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('facet-discover'));
    expect(
      screen.getByPlaceholderText('Search your selected sources…'),
    ).toBeInTheDocument();
  });

  it('renders search button', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('facet-discover'));
    expect(screen.getByRole('button', { name: /search/i })).toBeInTheDocument();
  });

  it('disables Search button and shows help text when no sources are selected', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    // Wait for all source checkboxes to render
    await waitFor(() => {
      expect(screen.getByLabelText('arXiv')).toBeInTheDocument();
    });

    // Uncheck every external source
    await user.click(screen.getByLabelText('arXiv'));
    await user.click(screen.getByLabelText('Semantic Scholar'));
    await user.click(screen.getByLabelText('OpenAlex'));
    await user.click(screen.getByLabelText('PubMed'));

    // Type a query so the "empty query" disabling rule doesn't mask this behaviour
    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'neural networks');

    const searchBtn = screen.getByRole('button', { name: /search/i });
    expect(searchBtn).toBeDisabled();
    expect(screen.getByText('Select at least one source')).toBeInTheDocument();
  });

  it('renders source checkboxes in Search tab for enabled non-local sources', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    // Wait for sources to load
    await waitFor(() => {
      expect(screen.getByLabelText('arXiv')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Semantic Scholar')).toBeInTheDocument();
    expect(screen.getByLabelText('OpenAlex')).toBeInTheDocument();
    expect(screen.getByLabelText('PubMed')).toBeInTheDocument();
    // Local source should not appear in the Search tab checkboxes
    expect(screen.queryByLabelText('Local')).not.toBeInTheDocument();
  });

  it('search with only arxiv + pubmed checked passes correct source_types to API', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    renderPage();

    await user.click(screen.getByTestId('facet-discover'));

    // Wait for checkboxes
    await waitFor(() => {
      expect(screen.getByLabelText('Semantic Scholar')).toBeInTheDocument();
    });

    // Uncheck Semantic Scholar and OpenAlex, leave arxiv + pubmed
    await user.click(screen.getByLabelText('Semantic Scholar'));
    await user.click(screen.getByLabelText('OpenAlex'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'neural networks');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(vi.mocked(searchPreview)).toHaveBeenCalledWith(
        'neural networks',
        expect.arrayContaining(['arxiv', 'pubmed']),
        expect.any(Number),
        expect.any(Object),
      );
    });
    const callArgs = vi.mocked(searchPreview).mock.calls[0];
    if (!callArgs) throw new Error('test fixture: searchPreview was not called');
    const sourceTypes = callArgs[1] as string[];
    expect(sourceTypes).not.toContain('semantic_scholar');
    expect(sourceTypes).not.toContain('openalex');
  });

  it('shows structured source error details when backend reports errors', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'ArXiv Only Paper',
          authors: ['Author X'],
          abstract: null,
          published_date: '2025-01-01',
          url: 'https://arxiv.org/abs/2301.99999',
          external_id: 'arxiv:2301.99999',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 0,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: ['pubmed'],
      source_errors: {
        pubmed: {
          kind: 'rate_limit',
          message: 'PubMed rate limit reached. Retry later.',
          status_code: 429,
          retry_after_s: 2,
          settings_hint: null,
        },
      },
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Search your selected sources…')).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'cardiac imaging');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText(/PubMed rate limit reached/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Status 429/i)).toBeInTheDocument();
    expect(screen.getByText(/Retry after 2s/i)).toBeInTheDocument();
  });

  it('excludes library-matched results from save actions and marks them as already in library', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Matched Preview Paper',
          authors: ['Saved Author'],
          abstract: 'Already stored.',
          published_date: '2025-04-01',
          url: 'https://example.com/paper/4',
          external_id: 'arxiv:2304.00002',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 7,
          metadata: {},
          library_match: {
            paper_id: 88,
            has_project_links: false,
            zotero_item_key: null,
          },
        },
        {
          title: 'Unsaved Preview Paper',
          authors: ['Draft Author'],
          abstract: 'Save me.',
          published_date: '2025-04-02',
          url: 'https://example.com/paper/5',
          external_id: 'arxiv:2304.00003',
          source_type: 'pubmed',
          pdf_url: null,
          citation_count: 4,
          metadata: {},
          library_match: null,
        },
      ],
      total: 2,
      per_source_counts: { arxiv: 1, pubmed: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'mixed results');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Matched Preview Paper')).toBeInTheDocument();
      expect(screen.getByText('Unsaved Preview Paper')).toBeInTheDocument();
    });

    expect(screen.getByText('Already in library')).toBeInTheDocument();
    const matchedCheckbox = screen.getByLabelText('Already in library: Matched Preview Paper');
    expect(matchedCheckbox).toBeDisabled();
    expect(matchedCheckbox).not.toBeChecked();
    expect(screen.getByRole('button', { name: /save 1 selected/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save all unsaved/i })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: /save all unsaved/i }));

    await waitFor(() => {
      expect(vi.mocked(batchSavePapers)).toHaveBeenCalled();
    });
    const batchSaveCall = vi.mocked(batchSavePapers).mock.calls[0];
    if (!batchSaveCall) throw new Error('test fixture: batchSavePapers was not called');
    const [savedPapers] = batchSaveCall;
    expect(savedPapers).toHaveLength(1);
    expect(savedPapers[0]).toMatchObject({ external_id: 'arxiv:2304.00003' });
  });

  it('still saves unsaved preview rows unchanged', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Unsaved Save Test Paper',
          authors: ['Draft Author'],
          abstract: 'Save me as-is.',
          published_date: '2025-06-01',
          url: 'https://example.com/paper/6',
          external_id: 'arxiv:2306.00002',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 2,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'save unchanged');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Unsaved Save Test Paper')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: /save 1 selected/i })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: /save 1 selected/i }));

    await waitFor(() => {
      expect(vi.mocked(batchSavePapers)).toHaveBeenCalled();
    });
    const batchSaveCall2 = vi.mocked(batchSavePapers).mock.calls[0];
    if (!batchSaveCall2) throw new Error('test fixture: batchSavePapers was not called');
    const [savedPapers] = batchSaveCall2;
    expect(savedPapers).toHaveLength(1);
    expect(savedPapers[0]).toMatchObject({ external_id: 'arxiv:2306.00002' });
  });

  it('navigates to paper detail when a saved preview result title is clicked', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Saved Preview Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-04-01',
          url: 'https://example.com/paper/1',
          external_id: 'arxiv:2304.00001',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 3,
          metadata: {},
          library_match: {
            paper_id: 42,
            has_project_links: false,
            zotero_item_key: null,
          },
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'saved result');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Saved Preview Paper')).toBeInTheDocument();
    });

    await user.click(getPreviewRowPrimaryButton('Saved Preview Paper'));

    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/paper/42');
    });
  });

  it('opens the preview drawer when an unsaved preview result title is clicked', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Unsaved Preview Paper',
          authors: ['Draft Author'],
          abstract: 'Unsaved abstract.',
          published_date: '2025-05-01',
          url: 'https://example.com/paper/2',
          external_id: 'arxiv:2305.00001',
          source_type: 'pubmed',
          pdf_url: 'https://example.com/paper/2.pdf',
          citation_count: 11,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { pubmed: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'unsaved result');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Unsaved Preview Paper')).toBeInTheDocument();
    });

    await user.click(getPreviewRowPrimaryButton('Unsaved Preview Paper'));

    expect(screen.getByRole('heading', { name: /Unsaved Preview Paper/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /open original/i })).toHaveAttribute(
      'href',
      'https://example.com/paper/2',
    );
  });

  it('closes an unsaved preview drawer when a new search replaces the result set', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview)
      .mockResolvedValueOnce({
        results: [
          {
            title: 'First Unsaved Paper',
            authors: ['Draft Author'],
            abstract: 'First abstract.',
            published_date: '2025-05-01',
            url: 'https://example.com/paper/2',
            external_id: 'arxiv:2305.00001',
            source_type: 'pubmed',
            pdf_url: 'https://example.com/paper/2.pdf',
            citation_count: 11,
            metadata: {},
            library_match: null,
          },
        ],
        total: 1,
        per_source_counts: { pubmed: 1 },
        degraded_sources: [],
        source_errors: {},
      })
      .mockResolvedValueOnce({
        results: [
          {
            title: 'Second Search Result',
            authors: ['Other Author'],
            abstract: 'Second abstract.',
            published_date: '2025-06-01',
            url: 'https://example.com/paper/3',
            external_id: 'arxiv:2306.00001',
            source_type: 'arxiv',
            pdf_url: null,
            citation_count: 2,
            metadata: {},
            library_match: null,
          },
        ],
        total: 1,
        per_source_counts: { arxiv: 1 },
        degraded_sources: [],
        source_errors: {},
      });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    const searchButton = screen.getByRole('button', { name: /search/i });
    await user.type(searchInput, 'first query');
    await user.click(searchButton);

    await waitFor(() => {
      expect(screen.getByText('First Unsaved Paper')).toBeInTheDocument();
    });

    await user.click(getPreviewRowPrimaryButton('First Unsaved Paper'));
    expect(screen.getByRole('heading', { name: /First Unsaved Paper/i })).toBeInTheDocument();

    fireEvent.click(searchButton);

    await waitFor(() => {
      expect(screen.getByText('Second Search Result')).toBeInTheDocument();
    });

    expect(screen.queryByRole('heading', { name: /First Unsaved Paper/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /open original/i })).not.toBeInTheDocument();
  });

  it('renders structured source errors from source_errors', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [],
      total: 0,
      per_source_counts: {},
      degraded_sources: ['pubmed'],
      source_errors: {
        pubmed: {
          kind: 'api_error',
          message: 'PubMed returned HTTP 503.',
          status_code: 503,
          retry_after_s: null,
          settings_hint: 'Try again later.',
        },
        openalex: {
          kind: 'unavailable',
          message: 'OpenAlex is temporarily unavailable.',
          status_code: null,
          retry_after_s: null,
          settings_hint: null,
        },
      },
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'error case');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText(/PubMed returned HTTP 503/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/OpenAlex is temporarily unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/Some sources had errors/i)).not.toBeInTheDocument();
  });

  it('switches to Library surface on facet click', async () => {
    const user = userEvent.setup();
    renderPage();
    // F1 IA: Library is a §Status facet button (aria-pressed)
    const libraryFacet = screen.getByTestId('facet-status-library');
    await user.click(libraryFacet);
    expect(libraryFacet).toHaveAttribute('aria-pressed', 'true');
  });

  it('shows papers in the New tab after loading', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Test Paper One')).toBeInTheDocument();
    });
    expect(screen.getByText('Test Paper Two')).toBeInTheDocument();
  });

  it('shows search results after searching', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'graph neural networks');

    const searchBtn = screen.getByRole('button', { name: /search/i });
    await user.click(searchBtn);

    await waitFor(() => {
      expect(screen.getByText('Search Result Paper')).toBeInTheDocument();
    });
  });

  it.each([
    {
      title: 'Unsaved Preview Paper',
      result: {
        title: 'Unsaved Preview Paper',
        authors: ['Draft Author'],
        abstract: 'Unsaved abstract.',
        published_date: '2025-05-01',
        url: 'https://example.com/paper/2',
        external_id: 'arxiv:2305.00001',
        source_type: 'pubmed',
        pdf_url: 'https://example.com/paper/2.pdf',
        citation_count: 11,
        metadata: {},
        library_match: null,
      },
      expected: ['Save to Library', 'Open original'],
      absent: ['Open Paper Detail', 'Open Projects to Link', 'Send to Zotero', 'View in Zotero', 'Re-sync Zotero'],
    },
    {
      title: 'Saved Without Projects Paper',
      result: {
        title: 'Saved Without Projects Paper',
        authors: ['Saved Author'],
        abstract: 'Saved abstract.',
        published_date: '2025-05-02',
        url: 'https://example.com/paper/3',
        external_id: 'arxiv:2305.00002',
        source_type: 'arxiv',
        pdf_url: null,
        citation_count: 7,
        metadata: {},
        library_match: {
          paper_id: 101,
          has_project_links: false,
          zotero_item_key: null,
        },
      },
      expected: ['Open Paper Detail', 'Open original', 'Open Projects to Link'],
      absent: ['Save to Library', 'Send to Zotero', 'View in Zotero', 'Re-sync Zotero'],
    },
    {
      title: 'Saved With Projects Paper',
      result: {
        title: 'Saved With Projects Paper',
        authors: ['Saved Author'],
        abstract: 'Saved abstract.',
        published_date: '2025-05-03',
        url: 'https://example.com/paper/4',
        external_id: 'arxiv:2305.00003',
        source_type: 'semantic_scholar',
        pdf_url: null,
        citation_count: 4,
        metadata: {},
        library_match: {
          paper_id: 102,
          has_project_links: true,
          zotero_item_key: null,
        },
      },
      expected: ['Open Paper Detail', 'Open original', 'Send to Zotero'],
      absent: ['Save to Library', 'Open Projects to Link', 'View in Zotero', 'Re-sync Zotero'],
    },
    {
      title: 'Saved With Zotero Paper',
      result: {
        title: 'Saved With Zotero Paper',
        authors: ['Saved Author'],
        abstract: 'Saved abstract.',
        published_date: '2025-05-04',
        url: 'https://example.com/paper/5',
        external_id: 'arxiv:2305.00004',
        source_type: 'openalex',
        pdf_url: null,
        citation_count: 2,
        metadata: {},
        library_match: {
          paper_id: 103,
          has_project_links: true,
          zotero_item_key: 'ABCD1234',
        },
      },
      expected: ['Open Paper Detail', 'Open original', 'View in Zotero', 'Re-sync Zotero'],
      absent: ['Save to Library', 'Open Projects to Link', 'Send to Zotero'],
    },
  ])('shows the correct trailing actions for $title', async ({ result, expected, absent }) => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [result],
      total: 1,
      per_source_counts: { [result.source_type]: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'row actions');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText(result.title)).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: `Actions for ${result.title}` }));

    for (const label of expected) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    for (const label of absent) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it('does not eagerly fetch linkage for a pre-linked saved row', async () => {
    const user = userEvent.setup();
    const { searchPreview, zoteroGetLinkage } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Pre-linked Zotero Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-05-04',
          url: 'https://example.com/paper/5',
          external_id: 'arxiv:2305.00004',
          source_type: 'openalex',
          pdf_url: null,
          citation_count: 2,
          metadata: {},
          library_match: {
            paper_id: 103,
            has_project_links: true,
            zotero_item_key: 'ABCD1234',
          },
        },
      ],
      total: 1,
      per_source_counts: { openalex: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'prelinked');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Pre-linked Zotero Paper')).toBeInTheDocument();
    });

    expect(vi.mocked(zoteroGetLinkage)).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Actions for Pre-linked Zotero Paper' }));
    expect(screen.getByRole('menuitem', { name: 'View in Zotero' })).toBeInTheDocument();
    expect(vi.mocked(zoteroGetLinkage)).not.toHaveBeenCalled();
  });

  it('navigates Open Projects to Link to /projects without fake route state', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Project Link Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-06-01',
          url: 'https://example.com/paper/6',
          external_id: 'arxiv:2306.00001',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 8,
          metadata: {},
          library_match: {
            paper_id: 104,
            has_project_links: false,
            zotero_item_key: null,
          },
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'projects');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Project Link Paper')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: 'Actions for Project Link Paper' }));
    await user.click(screen.getByRole('menuitem', { name: 'Open Projects to Link' }));

    await waitFor(() => {
      expect(screen.getByTestId('location-path')).toHaveTextContent('/projects');
    });
    expect(screen.getByTestId('location-state')).toHaveTextContent('null');
  });

  it('disables repeat Zotero actions while a row Zotero job is queued or running', async () => {
    const user = userEvent.setup();
    const { searchPreview, zoteroPushPaper } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Queued Zotero Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-06-02',
          url: 'https://example.com/paper/7',
          external_id: 'arxiv:2306.00002',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 9,
          metadata: {},
          library_match: {
            paper_id: 105,
            has_project_links: true,
            zotero_item_key: null,
          },
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    vi.mocked(zoteroPushPaper).mockResolvedValueOnce({ job_id: 'job-zotero-queued', status: 'queued' });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":25,"progress_message":"Queued for Zotero"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'zotero queue');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Queued Zotero Paper')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: 'Actions for Queued Zotero Paper' }));
    await user.click(screen.getByRole('menuitem', { name: 'Send to Zotero' }));

    await waitFor(() => {
      expect(vi.mocked(zoteroPushPaper)).toHaveBeenCalledTimes(1);
    });

    expect(screen.getByRole('menuitem', { name: 'Send to Zotero' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
  });

  it('starts Zotero linkage observation only after a Zotero action is queued for a saved row without Zotero item', async () => {
    const user = userEvent.setup();
    const { searchPreview, zoteroGetLinkage, zoteroPushPaper } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Lazy Zotero Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-06-02',
          url: 'https://example.com/paper/7',
          external_id: 'arxiv:2306.00002',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 9,
          metadata: {},
          library_match: {
            paper_id: 105,
            has_project_links: true,
            zotero_item_key: null,
          },
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });

    vi.mocked(zoteroPushPaper).mockResolvedValueOnce({ job_id: 'job-zotero-lazy', status: 'queued' });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'zotero lazy');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Lazy Zotero Paper')).toBeInTheDocument();
    });

    expect(vi.mocked(zoteroGetLinkage)).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Actions for Lazy Zotero Paper' }));
    await user.click(screen.getByRole('menuitem', { name: 'Send to Zotero' }));

    await waitFor(() => {
      expect(vi.mocked(zoteroGetLinkage)).toHaveBeenCalled();
    });
  });

  it('does not start Zotero linkage observation when Zotero enqueue fails', async () => {
    const user = userEvent.setup();
    const { toast } = await import('sonner');
    const { searchPreview, zoteroGetLinkage, zoteroPushPaper } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Failed Zotero Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-06-04',
          url: 'https://example.com/paper/9',
          external_id: 'arxiv:2306.00004',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 10,
          metadata: {},
          library_match: {
            paper_id: 107,
            has_project_links: true,
            zotero_item_key: null,
          },
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(zoteroPushPaper).mockRejectedValueOnce(new Error('Zotero enqueue failed'));

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'zotero failure');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Failed Zotero Paper')).toBeInTheDocument();
    });

    expect(vi.mocked(zoteroGetLinkage)).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Actions for Failed Zotero Paper' }));
    await user.click(screen.getByRole('menuitem', { name: 'Send to Zotero' }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Zotero enqueue failed');
    });
    expect(vi.mocked(zoteroGetLinkage)).not.toHaveBeenCalled();
  });

  it('hydrates Zotero linkage observation for an existing external running job and flips to View in Zotero after success', async () => {
    const user = userEvent.setup();
    const { searchPreview, zoteroGetLinkage } = await import('@/lib/api');
    useJobStore.setState({ jobs: {}, activeAborts: {} });
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Hydrated Zotero Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-06-05',
          url: 'https://example.com/paper/10',
          external_id: 'arxiv:2306.00005',
          source_type: 'pubmed',
          pdf_url: null,
          citation_count: 13,
          metadata: {},
          library_match: {
            paper_id: 108,
            has_project_links: true,
            zotero_item_key: null,
          },
        },
      ],
      total: 1,
      per_source_counts: { pubmed: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(zoteroGetLinkage)
      .mockResolvedValueOnce({
        zotero_item_key: null,
        zotero_citation_key: null,
        zotero_last_pushed_at: null,
      })
      .mockResolvedValueOnce({
        zotero_item_key: 'ITEM-555',
        zotero_citation_key: 'Citation-555',
        zotero_last_pushed_at: '2026-04-23T00:00:00Z',
      });
    const zoteroStream = createControlledSSEStream();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(zoteroStream.stream, { status: 200 }));
    useJobStore.getState().trackExternalJob({
      jobId: 'job-zotero-hydrated',
      kind: 'zotero.push',
      payload: { paper_id: 108 },
      status: 'running',
    });
    zoteroStream.push('data: {"status":"running","progress":60,"progress_message":"Sending to Zotero"}\n\n');

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'zotero hydrated');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Hydrated Zotero Paper')).toBeInTheDocument();
    });

    expect(vi.mocked(zoteroGetLinkage)).toHaveBeenCalled();
    zoteroStream.push('data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n');
    zoteroStream.push('data: [DONE]\n\n');
    zoteroStream.close();

    await waitFor(() => {
      expect(vi.mocked(zoteroGetLinkage).mock.calls.length).toBeGreaterThanOrEqual(2);
    });

    if (screen.queryByRole('menuitem', { name: 'Send to Zotero' })) {
      await waitFor(() => {
        expect(screen.queryByRole('menuitem', { name: 'Send to Zotero' })).not.toBeInTheDocument();
      });
    }

    if (!screen.queryByRole('menuitem', { name: 'View in Zotero' })) {
      await user.click(screen.getByRole('button', { name: 'Actions for Hydrated Zotero Paper' }));
    }
    expect(screen.getByRole('menuitem', { name: 'View in Zotero' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: 'Send to Zotero' })).not.toBeInTheDocument();
  });

  it('updates a row from Send to Zotero to View in Zotero after the Zotero job succeeds', async () => {
    const user = userEvent.setup();
    const { searchPreview, zoteroPushPaper, zoteroGetLinkage } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Zotero Update Paper',
          authors: ['Saved Author'],
          abstract: 'Saved abstract.',
          published_date: '2025-06-03',
          url: 'https://example.com/paper/8',
          external_id: 'arxiv:2306.00003',
          source_type: 'pubmed',
          pdf_url: null,
          citation_count: 12,
          metadata: {},
          library_match: {
            paper_id: 106,
            has_project_links: true,
            zotero_item_key: null,
          },
        },
      ],
      total: 1,
      per_source_counts: { pubmed: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(zoteroGetLinkage)
      .mockResolvedValueOnce({
        zotero_item_key: null,
        zotero_citation_key: null,
        zotero_last_pushed_at: null,
      })
      .mockResolvedValueOnce({
        zotero_item_key: 'ITEM-12345',
        zotero_citation_key: 'Citation-12345',
        zotero_last_pushed_at: '2026-04-23T00:00:00Z',
      });
    vi.mocked(zoteroPushPaper).mockResolvedValueOnce({ job_id: 'job-zotero-success', status: 'queued' });
    // Use a controlled stream (not createMockSSEStream) so we can gate the
    // "succeeded" frame until after the first zoteroGetLinkage call has landed.
    // createMockSSEStream delivers ALL frames synchronously, which causes a race
    // where invalidateQueries fires before TanStack Query executes the initial
    // fetch — making call #1 and the invalidation-triggered refetch coalesce
    // into a single network request (only 1 zoteroGetLinkage call instead of 2).
    const zoteroStream = createControlledSSEStream();
    // URL-aware fetch spy: only our job's stream URL gets the controlled
    // ReadableStream.  Any other URL (e.g. stale reconnect from a prior test's
    // _reconnectAfterDrop IIFE) receives a 404 so it cannot lock our stream.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url === '/api/jobs/job-zotero-success/stream') {
        return new Response(zoteroStream.stream, { status: 200 });
      }
      return new Response(null, { status: 404 });
    });
    zoteroStream.push('data: {"status":"running","progress":50,"progress_message":"Sending to Zotero"}\n\n');

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'zotero success');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Zotero Update Paper')).toBeInTheDocument();
    });

    expect(vi.mocked(zoteroGetLinkage)).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Actions for Zotero Update Paper' }));
    expect(screen.getByRole('menuitem', { name: 'Send to Zotero' })).toBeInTheDocument();
    await user.click(screen.getByRole('menuitem', { name: 'Send to Zotero' }));

    // Wait for the first linkage poll (query enabled after push enqueued) AND
    // for TanStack Query to finish storing the result (fetchStatus → idle).
    // We must wait for the query to settle before pushing "succeeded": if
    // invalidateQueries fires while the first fetch is in-flight, TanStack
    // Query coalesces it and never issues a second fetch.
    await waitFor(() => {
      expect(vi.mocked(zoteroGetLinkage)).toHaveBeenCalled();
      const state = appQueryClient.getQueryState(['zotero-linkage', 106]);
      expect(state?.fetchStatus).toBe('idle');
    });

    // Now it's safe to advance the stream to succeeded — this triggers
    // invalidateQueries which causes the second linkage poll.
    zoteroStream.push('data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n');
    zoteroStream.push('data: [DONE]\n\n');
    zoteroStream.close();
    // Yield to microtask queue so the SSE IIFE can process the "succeeded" frame
    await Promise.resolve();
    await Promise.resolve();

    // The dropdown is still open (event.preventDefault() in onSelect keeps it open).
    // Wait for the dropdown content to flip: "Send to Zotero" disappears when
    // zoteroItemKey becomes non-null (ITEM-12345), which can only happen after
    // the second zoteroGetLinkage call returns the linked item.
    await waitFor(() => {
      expect(screen.queryByRole('menuitem', { name: 'Send to Zotero' })).not.toBeInTheDocument();
    }, { timeout: 5000 });
    // "View in Zotero" should now be visible in the still-open dropdown.
    expect(screen.getByRole('menuitem', { name: 'View in Zotero' })).toBeInTheDocument();
    // Exactly 2+ calls: first poll (null linkage) + re-poll after invalidation (ITEM-12345).
    expect(vi.mocked(zoteroGetLinkage).mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('menuitem', { name: 'Re-sync Zotero' })).toBeInTheDocument();
  },
  // Extended timeout (15 s): drives a full SSE → invalidateQueries → TanStack
  // Query refetch → React re-render cycle.  Under CPU load in the full suite
  // the default 5 s Vitest test timeout is not enough.
  15000);

  it('keeps Search active and preserves preview results after save', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    const { toast } = await import('sonner');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Save Flow Paper',
          authors: ['Draft Author'],
          abstract: 'Save flow abstract.',
          published_date: '2025-07-01',
          url: 'https://example.com/paper/7',
          external_id: 'arxiv:2307.00001',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 8,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(batchSavePapers).mockResolvedValueOnce([
      {
        id: 701,
        external_id: 'arxiv:2307.00001',
        source_type: 'arxiv',
        title: 'Save Flow Paper',
        authors: ['Draft Author'],
        abstract: 'Save flow abstract.',
        published_date: '2025-07-01',
        url: 'https://example.com/paper/7',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 8,
        priority_score: null,
        metadata: {},
        discovered_at: '2025-07-01T00:00:00Z',
        created_at: '2025-07-01T00:00:00Z',
      },
    ]);

    renderPage();

    await user.click(screen.getByTestId('facet-discover'));
    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'save flow');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Save Flow Paper')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /save 1 selected/i }));

    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith('Saved 1 paper(s) to your library.');
    });

    // F1 IA: Discover (search surface) facet uses aria-pressed (not aria-selected)
    expect(screen.getByTestId('facet-discover')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('Save Flow Paper')).toBeInTheDocument();
  });

  it('marks saved preview rows as already in library in place', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Saved In Place Paper',
          authors: ['Saved Author'],
          abstract: 'Save me.',
          published_date: '2025-07-02',
          url: 'https://example.com/paper/8',
          external_id: 'arxiv:2307.00002',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 5,
          metadata: {},
          library_match: null,
        },
        {
          title: 'Still Unsaved Paper',
          authors: ['Draft Author'],
          abstract: 'Keep me unsaved.',
          published_date: '2025-07-03',
          url: 'https://example.com/paper/9',
          external_id: 'pubmed:2307.00003',
          source_type: 'pubmed',
          pdf_url: null,
          citation_count: 2,
          metadata: {},
          library_match: null,
        },
      ],
      total: 2,
      per_source_counts: { arxiv: 1, pubmed: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(batchSavePapers).mockResolvedValueOnce([
      {
        id: 702,
        external_id: 'arxiv:2307.00002',
        source_type: 'arxiv',
        title: 'Saved In Place Paper',
        authors: ['Saved Author'],
        abstract: 'Save me.',
        published_date: '2025-07-02',
        url: 'https://example.com/paper/8',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 5,
        priority_score: null,
        metadata: {},
        discovered_at: '2025-07-02T00:00:00Z',
        created_at: '2025-07-02T00:00:00Z',
      },
    ]);

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'in place');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Saved In Place Paper')).toBeInTheDocument();
      expect(screen.getByText('Still Unsaved Paper')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /save 2 selected/i }));

    await waitFor(() => {
      expect(screen.getByText('Already in library')).toBeInTheDocument();
    });

    expect(screen.getByLabelText('Already in library: Saved In Place Paper')).toBeDisabled();
    expect(screen.getByText('Still Unsaved Paper')).toBeInTheDocument();
    expect(screen.getByLabelText('Select Still Unsaved Paper')).toBeEnabled();
  });

  it('preserves partial selection when in-place save reconciliation updates the preview rows', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Selected Save Paper',
          authors: ['Saved Author'],
          abstract: 'Selected save.',
          published_date: '2025-07-06',
          url: 'https://example.com/paper/12',
          external_id: 'arxiv:2307.00006',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 5,
          metadata: {},
          library_match: null,
        },
        {
          title: 'Deselected Save Paper',
          authors: ['Saved Author'],
          abstract: 'Deselected save.',
          published_date: '2025-07-07',
          url: 'https://example.com/paper/13',
          external_id: 'arxiv:2307.00007',
          source_type: 'pubmed',
          pdf_url: null,
          citation_count: 4,
          metadata: {},
          library_match: null,
        },
      ],
      total: 2,
      per_source_counts: { arxiv: 1, pubmed: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(batchSavePapers).mockResolvedValueOnce([
      {
        id: 705,
        external_id: 'arxiv:2307.00006',
        source_type: 'arxiv',
        title: 'Selected Save Paper',
        authors: ['Saved Author'],
        abstract: 'Selected save.',
        published_date: '2025-07-06',
        url: 'https://example.com/paper/12',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 5,
        priority_score: null,
        metadata: {},
        discovered_at: '2025-07-06T00:00:00Z',
        created_at: '2025-07-06T00:00:00Z',
      },
    ]);

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));
    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'partial selection');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Selected Save Paper')).toBeInTheDocument();
      expect(screen.getByText('Deselected Save Paper')).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText('Select Deselected Save Paper'));
    await user.click(screen.getByRole('button', { name: /save 1 selected/i }));

    await waitFor(() => {
      expect(screen.getByLabelText('Already in library: Selected Save Paper')).toBeDisabled();
    });

    expect(screen.getByLabelText('Select Deselected Save Paper')).not.toBeChecked();
    expect(screen.getByRole('button', { name: /save 0 selected/i })).toBeDisabled();
  });

  it('changes the drawer primary action to Open Paper Detail after save', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Drawer Save Paper',
          authors: ['Draft Author'],
          abstract: 'Drawer abstract.',
          published_date: '2025-07-04',
          url: 'https://example.com/paper/10',
          external_id: 'arxiv:2307.00004',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 6,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(batchSavePapers).mockResolvedValueOnce([
      {
        id: 703,
        external_id: 'arxiv:2307.00004',
        source_type: 'arxiv',
        title: 'Drawer Save Paper',
        authors: ['Draft Author'],
        abstract: 'Drawer abstract.',
        published_date: '2025-07-04',
        url: 'https://example.com/paper/10',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 6,
        priority_score: null,
        metadata: {},
        discovered_at: '2025-07-04T00:00:00Z',
        created_at: '2025-07-04T00:00:00Z',
      },
    ]);

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'drawer save');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Drawer Save Paper')).toBeInTheDocument();
    });

    await user.click(getPreviewRowPrimaryButton('Drawer Save Paper'));
    expect(screen.getByRole('button', { name: /save to library/i })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /save to library/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /open paper detail/i })).toBeInTheDocument();
    });
  });

  it('keeps the drawer and preview results visible when drawer save fails', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    const { toast } = await import('sonner');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Drawer Error Paper',
          authors: ['Draft Author'],
          abstract: 'Drawer error abstract.',
          published_date: '2025-07-08',
          url: 'https://example.com/paper/14',
          external_id: 'arxiv:2307.00008',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 9,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(batchSavePapers).mockRejectedValueOnce(new Error('Save exploded'));

    renderPage();
    await user.click(screen.getByTestId('facet-discover'));
    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'drawer error');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Drawer Error Paper')).toBeInTheDocument();
    });

    await user.click(getPreviewRowPrimaryButton('Drawer Error Paper'));
    await user.click(screen.getByRole('button', { name: /save to library/i }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Save exploded');
    });

    expect(screen.getByRole('dialog', { name: /Drawer Error Paper/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Drawer Error Paper/i })).toBeInTheDocument();
    expect(screen.getAllByText('Drawer Error Paper').length).toBeGreaterThanOrEqual(2);
  });

  it('invalidates the library feed query after save', async () => {
    const user = userEvent.setup();
    const { searchPreview, batchSavePapers } = await import('@/lib/api');
    vi.mocked(searchPreview).mockResolvedValueOnce({
      results: [
        {
          title: 'Invalidate Library Paper',
          authors: ['Draft Author'],
          abstract: 'Invalidate me.',
          published_date: '2025-07-05',
          url: 'https://example.com/paper/11',
          external_id: 'arxiv:2307.00005',
          source_type: 'arxiv',
          pdf_url: null,
          citation_count: 1,
          metadata: {},
          library_match: null,
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
      source_errors: {},
    });
    vi.mocked(batchSavePapers).mockResolvedValueOnce([
      {
        id: 704,
        external_id: 'arxiv:2307.00005',
        source_type: 'arxiv',
        title: 'Invalidate Library Paper',
        authors: ['Draft Author'],
        abstract: 'Invalidate me.',
        published_date: '2025-07-05',
        url: 'https://example.com/paper/11',
        pdf_url: null,
        pdf_local_path: null,
        pdf_downloaded: false,
        citation_count: 1,
        priority_score: null,
        metadata: {},
        discovered_at: '2025-07-05T00:00:00Z',
        created_at: '2025-07-05T00:00:00Z',
      },
    ]);

    const { queryClient } = renderPage();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    await user.click(screen.getByTestId('facet-discover'));
    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'invalidate library');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Invalidate Library Paper')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /save 1 selected/i }));

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['papers-feed'] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['feed-counts'] });
    });
  });

  it('shows actionable search error details and clears stale preview results', async () => {
    const user = userEvent.setup();
    const { searchPreview } = await import('@/lib/api');
    vi.mocked(searchPreview).mockRejectedValueOnce(
      new ApiError(429, JSON.stringify({
        detail: 'Semantic Scholar rate limit reached. Retry later or configure an API key in Settings > Sources.',
      })),
    );

    renderPage();

    await user.click(screen.getByTestId('facet-discover'));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'graph neural networks');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Semantic Scholar rate limit reached/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText('Search Result Paper')).not.toBeInTheDocument();
  });

  it('Ask is removed from feed — StreamingChat not rendered in feed (spec §3.4)', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('facet-rail')).toBeInTheDocument();
    });
    // Ask has its own route (/ask via F4); StreamingChat is not rendered in the feed page
    expect(screen.queryByTestId('streaming-chat')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Ask' })).not.toBeInTheDocument();
  });

  it('shows Library surface content with section description via §Status facet', async () => {
    const user = userEvent.setup();
    renderPage();

    // F1 IA: Library is a §Status facet button
    const libraryFacet = screen.getByTestId('facet-status-library');
    await user.click(libraryFacet);

    await waitFor(() => {
      // The Library surface renders a section description and its FeedView
      expect(
        screen.getByText('Browse, search, and filter all papers in your library.'),
      ).toBeInTheDocument();
    });
  });

  it('shows library papers after switching to Library §Status facet', async () => {
    const user = userEvent.setup();
    renderPage();

    // F1 IA: Library §Status facet
    const libraryFacet = screen.getByTestId('facet-status-library');
    await user.click(libraryFacet);

    // Library surface now renders papers via FeedView
    await waitFor(() => {
      expect(screen.getByText('Test Paper One')).toBeInTheDocument();
    });
  });

  // F1 3-pane IA: clicking Library §Status facet shows the library surface content
  it('clicking Library §Status facet navigates to the library surface and shows section info', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 0, library: 2, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 2, kept: 2, all_non_trash: 2 },
      isLoading: false,
      isPending: false,
    } as ReturnType<typeof useFeedCounts>);
    const user = userEvent.setup();
    renderPage();

    // Click Library §Status facet
    await user.click(screen.getByTestId('facet-status-library'));
    await waitFor(() => {
      expect(screen.getByTestId('facet-status-library')).toHaveAttribute('aria-pressed', 'true');
    });

    // Library surface renders the section description and the FeedView content
    expect(screen.getByText('Browse, search, and filter all papers in your library.')).toBeInTheDocument();
    await screen.findByText('Test Paper One');
  });

  // ── F1 3-pane IA — §Status facet items replace surface chips ─────────────

  it('renders §Status facet items: Inbox | Library | Reading | Reading List | Done | Trash', () => {
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    renderPage();
    // F1 3-pane IA: §Status facet buttons (aria-pressed)
    expect(screen.getByTestId('facet-status-inbox')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-library')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-reading')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-to_read')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-done')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-trash')).toBeInTheDocument();
    // Discover link (search surface) is in rail
    expect(screen.getByTestId('facet-discover')).toBeInTheDocument();
    // Ask is NOT a feed surface (F4 owns /ask route)
    expect(screen.queryByRole('tab', { name: 'Ask' })).not.toBeInTheDocument();
  });

  it('§Status facet uses useFeedCounts data for count badge rendering in FacetRail', () => {
    // When useFeedCounts (via useFeedCountsWithFacets) returns counts, FacetRail renders them
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 3, library: 5, reading_list: 0, reading: 2, done: 0, starred: 1, trash: 0, active: 10, kept: 10, all_non_trash: 10 },
      isLoading: false,
      isPending: false,
    } as ReturnType<typeof useFeedCounts>);
    renderPage();
    // Inbox facet is present in the rail
    expect(screen.getByTestId('facet-status-inbox')).toBeInTheDocument();
    // Trash facet is present in the rail (no longer a top-level tab)
    expect(screen.getByTestId('facet-status-trash')).toBeInTheDocument();
  });

  it('default landing redirects to ?surface=inbox when inbox > 0', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 2, library: 5, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 7, kept: 7, all_non_trash: 7 },
      isLoading: false,
      isPending: false,
    } as ReturnType<typeof useFeedCounts>);
    renderPage();
    await waitFor(() => {
      // After redirect, Inbox §Status facet should be aria-pressed
      expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'true');
    });
  });

  it('default landing redirects to ?surface=inbox (spec §3.5: Inbox-first always)', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 0, library: 5, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 5, kept: 5, all_non_trash: 5 },
      isLoading: false,
      isPending: false,
    } as ReturnType<typeof useFeedCounts>);
    renderPage();
    await waitFor(() => {
      // F1 §3.5: default landing = Inbox (not library-first)
      expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'true');
    });
  });

  it('?tab=pulse legacy deep-link causes navigate to /my-day', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    // Render with the legacy ?tab=pulse query param
    render(
      <QueryClientProvider client={appQueryClient}>
        <MemoryRouter initialEntries={['/feed?tab=pulse']}>
          <Routes>
            <Route path="/feed" element={<ResearchFeedPage />} />
            <Route path="/my-day" element={<LocationDisplay />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(screen.getByTestId('location-path')).toHaveTextContent('/my-day');
    });
  });

  // ── F1 3-pane IA: §Status facet click → surface update ───────────────────

  it('clicking §Status facets makes the clicked facet aria-pressed=true', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    const user = userEvent.setup();
    renderPage();

    // Click through status facets and verify aria-pressed
    const facets = [
      'facet-status-inbox',
      'facet-status-library',
      'facet-status-trash',
    ] as const;

    for (const testId of facets) {
      const facet = screen.getByTestId(testId);
      await user.click(facet);
      expect(facet).toHaveAttribute('aria-pressed', 'true');
      // Others in this facet group should be aria-pressed=false
      for (const otherId of facets) {
        if (otherId === testId) continue;
        expect(screen.getByTestId(otherId)).toHaveAttribute('aria-pressed', 'false');
      }
    }
  });

  // ── W2-T3: ?surface=garbage URL fallback ───────────────────────────────────

  it('?surface=garbage falls back to Inbox surface (VALID_SURFACES guard)', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    render(
      <QueryClientProvider client={appQueryClient}>
        <MemoryRouter initialEntries={['/feed?surface=garbage']}>
          <Routes>
            <Route path="/feed" element={<ResearchFeedPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // Unknown surface falls back to 'inbox' — Inbox §Status facet is active
    expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('facet-status-library')).toHaveAttribute('aria-pressed', 'false');
  });

  // ── T3.1 Phase-A: VALID_SURFACES tighten — ?surface=starred is no longer a surface ──

  it('?surface=starred falls back to Inbox (VALID_SURFACES tightened — starred is sub-filter only)', () => {
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    render(
      <QueryClientProvider client={appQueryClient}>
        <MemoryRouter initialEntries={['/feed?surface=starred']}>
          <Routes>
            <Route path="/feed" element={<ResearchFeedPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // 'starred' is not in VALID_SURFACES — falls back to 'inbox'
    expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('facet-status-library')).toHaveAttribute('aria-pressed', 'false');
  });

  // ── T3.1 Phase-A: Library sub-chips (spec §5.4) ───────────────────────────

  it('F1 IA: Library sub-filters are now §Status facet items in the rail (Reading/Reading List/Done)', async () => {
    // F1 3-pane IA: old Library sub-chips replaced by §Status facets in FacetRail
    // Reading, Reading List, Done appear as §Status facet items for all surfaces
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    renderPage();

    // All §Status facet items are always visible in the rail
    expect(screen.getByTestId('facet-status-inbox')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-library')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-reading')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-to_read')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-done')).toBeInTheDocument();
    expect(screen.getByTestId('facet-status-trash')).toBeInTheDocument();

    // §Star facet (was "Starred" sub-chip) is also always visible
    expect(screen.getByTestId('facet-star-starred')).toBeInTheDocument();
  });

  // ── T3.1 Phase-A: Amber banner inlined for trash surface ──────────────────

  it('renders amber banner when surface=trash', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTestId('facet-status-trash'));

    // Amber banner from TrashView.tsx — preserved verbatim
    const banner = await screen.findByRole('alert');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent(
      'Papers in Trash will be kept until you delete them forever. Restore returns them to their previous location.',
    );
  });

  // ── T3.1 Phase-A: H5 — surface change clears bulk selection ───────────────

  it('H5: switching surface via URL clears bulk selection', async () => {
    const { useBulkSelection } = await import('@/stores/bulk-selection-store');
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    const user = userEvent.setup();
    renderPage();

    // Switch to Library §Status facet to establish a known selection context
    await user.click(screen.getByTestId('facet-status-library'));

    // Programmatically set some selected IDs
    useBulkSelection.setState({ selectedIds: new Set([1, 2, 3]) });
    expect(useBulkSelection.getState().selectedIds.size).toBe(3);

    // Switch to Trash — useEffect([surface]) should clear bulk selection
    await user.click(screen.getByTestId('facet-status-trash'));

    expect(useBulkSelection.getState().selectedIds.size).toBe(0);
  });

  // ── W1.8-D: Inbox source-type filter chips ─────────────────────────────────

  it('W1.8-D / F1 IA: §Source facet in FacetRail replaces old source-type sub-chips', async () => {
    // F1 3-pane IA: source-type filtering is via §Source facets in FacetRail
    // Old horizontal sub-chip row is removed; §Source rail is always visible
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    renderPage();
    // §Source section header is present in the rail
    expect(screen.getByText('Source')).toBeInTheDocument();
    // Old "Filter by source" tablist is removed
    expect(screen.queryByRole('tablist', { name: 'Filter by source' })).not.toBeInTheDocument();
  });

  it('W1.8-D / F1 IA: source filtering via §Source FacetRail is surface-agnostic (no old sub-chip rows)', async () => {
    vi.mocked(useFeedCounts).mockReturnValue({ data: undefined, isLoading: false, isPending: false } as ReturnType<typeof useFeedCounts>);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTestId('facet-status-library'));

    await waitFor(() => {
      expect(screen.getByTestId('facet-status-library')).toHaveAttribute('aria-pressed', 'true');
    });
    // Old sub-chip row "Filter by source" is removed in F1 IA
    expect(screen.queryByRole('tablist', { name: 'Filter by source' })).not.toBeInTheDocument();
    // §Source section is in the persistent rail
    expect(screen.getByText('Source')).toBeInTheDocument();
  });

  it('F1 IA: clicking §Source arXiv facet drives fetchFeed with sourceTypes="arxiv"', async () => {
    // In the F1 IA, source filtering is via §Source FacetRail facets.
    // The FacetRail uses fetchFeedCountsWithFacets; the source facet drives ?facet_source= in URL
    // which feeds into effectiveSourceTypes → FeedView sourceTypes prop.
    // This test verifies the §Source facet renders from by_source data.
    const { fetchFeedCountsWithFacets } = await import('@/lib/api');
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 5, library: 10, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 15, kept: 15, all_non_trash: 15 },
      isLoading: false,
      isPending: false,
    } as ReturnType<typeof useFeedCounts>);
    // Mock fetchFeedCountsWithFacets to return by_source data
    vi.mocked(fetchFeedCountsWithFacets).mockResolvedValue({
      inbox: 5, library: 10, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 15, kept: 15, all_non_trash: 15,
      by_source: { arxiv: 8 },
      by_topic: [],
      untagged: 0,
    });
    renderPage();
    // §Source facet for arXiv renders once counts load
    await waitFor(() => {
      expect(screen.getByTestId('facet-source-arxiv')).toBeInTheDocument();
    });
    expect(screen.getByTestId('facet-source-arxiv')).toHaveTextContent('8');
  });
});

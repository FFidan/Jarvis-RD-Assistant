import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { ApiError } from '@/lib/api';
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
    markPaperRead: vi.fn().mockResolvedValue({ status: 'ok' }),
    discoverPapers: vi.fn().mockResolvedValue([]),
    scanLocalPdfs: vi.fn().mockResolvedValue({ imported: 1, skipped: 0, scanned: 1 }),
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
  beforeEach(() => {
    vi.clearAllMocks();
    useJobStore.setState({ jobs: {}, activeAborts: {} });
    appQueryClient.clear();
  });

  it('renders the page heading', () => {
    renderPage();
    expect(screen.getByText('Research Feed')).toBeInTheDocument();
  });

  it('renders both tab triggers (Inbox and Library)', () => {
    renderPage();
    expect(screen.getByRole('tab', { name: 'Inbox' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Library' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Search' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Ask' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Pulse' })).toBeInTheDocument();
  });

  it('defaults to Library tab active', () => {
    renderPage();
    const libraryTab = screen.getByRole('tab', { name: 'Library' });
    expect(libraryTab).toHaveAttribute('data-state', 'active');
  });

  it('renders the Ask tab heading and description', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('tab', { name: 'Ask' }));
    expect(screen.getByText('Ask Questions')).toBeInTheDocument();
    expect(screen.getByText(/Get answers synthesised from your entire library/i)).toBeInTheDocument();
  });

  it('renders the search input with updated placeholder', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('tab', { name: 'Search' }));
    expect(
      screen.getByPlaceholderText('Search your selected sources…'),
    ).toBeInTheDocument();
  });

  it('renders search button', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('tab', { name: 'Search' }));
    expect(screen.getByRole('button', { name: /search/i })).toBeInTheDocument();
  });

  it('disables Search button and shows help text when no sources are selected', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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

    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    const [savedPapers] = vi.mocked(batchSavePapers).mock.calls[0];
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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    const [savedPapers] = vi.mocked(batchSavePapers).mock.calls[0];
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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

    const searchInput = screen.getByPlaceholderText('Search your selected sources…');
    await user.type(searchInput, 'error case');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText(/PubMed returned HTTP 503/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/OpenAlex is temporarily unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/Some sources had errors/i)).not.toBeInTheDocument();
  });

  it('switches to Library tab on click', async () => {
    const user = userEvent.setup();
    renderPage();
    const libraryTab = screen.getByRole('tab', { name: 'Library' });
    await user.click(libraryTab);
    expect(libraryTab).toHaveAttribute('data-state', 'active');
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

    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":50,"progress_message":"Sending to Zotero"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    renderPage();
    await user.click(screen.getByRole('tab', { name: 'Search' }));

    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'zotero success');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Zotero Update Paper')).toBeInTheDocument();
    });

    expect(vi.mocked(zoteroGetLinkage)).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Actions for Zotero Update Paper' }));
    expect(screen.getByRole('menuitem', { name: 'Send to Zotero' })).toBeInTheDocument();
    await user.click(screen.getByRole('menuitem', { name: 'Send to Zotero' }));

    await waitFor(() => {
      expect(vi.mocked(zoteroGetLinkage).mock.calls.length).toBeGreaterThanOrEqual(2);
    });

    await waitFor(() => {
      expect(screen.queryByRole('menuitem', { name: 'Send to Zotero' })).not.toBeInTheDocument();
    });
    if (!screen.queryByRole('menuitem', { name: 'View in Zotero' })) {
      await user.click(screen.getByRole('button', { name: 'Actions for Zotero Update Paper' }));
    }
    expect(screen.getByRole('menuitem', { name: 'View in Zotero' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Re-sync Zotero' })).toBeInTheDocument();
  });

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

    await user.click(screen.getByRole('tab', { name: 'Search' }));
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

    expect(screen.getByRole('tab', { name: 'Search' })).toHaveAttribute('data-state', 'active');
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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));
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
    await user.click(screen.getByRole('tab', { name: 'Search' }));

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
    await user.click(screen.getByRole('tab', { name: 'Search' }));
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

    await user.click(screen.getByRole('tab', { name: 'Search' }));
    await user.type(screen.getByPlaceholderText('Search your selected sources…'), 'invalidate library');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText('Invalidate Library Paper')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /save 1 selected/i }));

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['feed', 'library'] });
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

    await user.click(screen.getByRole('tab', { name: 'Search' }));

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

  it('shows StreamingChat with cross-paper scope in Ask tab', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('tab', { name: 'Ask' }));

    await waitFor(() => {
      expect(screen.getByTestId('streaming-chat')).toBeInTheDocument();
    });
    expect(screen.getByTestId('streaming-chat')).toHaveAttribute(
      'data-scope',
      'cross-paper',
    );
  });

  it('shows Library tab content with import section', async () => {
    const user = userEvent.setup();
    renderPage();

    const libraryTab = screen.getByRole('tab', { name: 'Library' });
    await user.click(libraryTab);

    await waitFor(() => {
      expect(screen.getByText('Import local PDFs')).toBeInTheDocument();
    });
  });

  it('shows library papers with filter input after switching to Library tab', async () => {
    const user = userEvent.setup();
    renderPage();

    const libraryTab = screen.getByRole('tab', { name: 'Library' });
    await user.click(libraryTab);

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText('Filter by title, abstract, or author...'),
      ).toBeInTheDocument();
    });
  });
});

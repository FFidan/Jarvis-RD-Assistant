import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { ApiError } from '@/lib/api';

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
        is_read: false,
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
        is_read: false,
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
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: [],
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ResearchFeedPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ResearchFeedPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it('shows degraded sources warning when backend reports errors', async () => {
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
        },
      ],
      total: 1,
      per_source_counts: { arxiv: 1 },
      degraded_sources: ['pubmed'],
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
      expect(screen.getByText(/Some sources had errors/i)).toBeInTheDocument();
    });
    // The warning banner should reference the degraded source by name
    const warningEl = screen.getByText(/Some sources had errors/i).closest('div');
    expect(warningEl?.textContent).toMatch(/PubMed/i);
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

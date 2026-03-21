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
    searchPreview: vi.fn().mockResolvedValue([
    {
      title: 'Search Result Paper',
      authors: ['Search Author'],
      abstract: 'A search result abstract.',
      published_date: '2025-03-01',
      url: 'https://arxiv.org/abs/2303.00001',
      external_id: 'arxiv:2303.00001',
      source_type: 'arxiv',
    },
  ]),
    batchSavePapers: vi.fn().mockResolvedValue([{ id: 1, title: 'Saved Paper' }]),
    markPaperRead: vi.fn().mockResolvedValue({ status: 'ok' }),
    discoverPapers: vi.fn().mockResolvedValue([]),
    scanLocalPdfs: vi.fn().mockResolvedValue({ imported: 1, skipped: 0, scanned: 1 }),
    batchProcessPapers: vi.fn().mockResolvedValue({
      queued: 5,
      total_unprocessed: 10,
      skipped_missing_pdf: 2,
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

  it('renders both tab triggers (New and Library)', () => {
    renderPage();
    expect(screen.getByRole('tab', { name: 'New' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Library' })).toBeInTheDocument();
  });

  it('defaults to New tab active', () => {
    renderPage();
    const newTab = screen.getByRole('tab', { name: 'New' });
    expect(newTab).toHaveAttribute('data-state', 'active');
  });

  it('renders the cross-paper chat expander', () => {
    renderPage();
    expect(screen.getByText('Ask across all papers')).toBeInTheDocument();
  });

  it('renders the search input', () => {
    renderPage();
    expect(
      screen.getByPlaceholderText('Search arXiv or Semantic Scholar...'),
    ).toBeInTheDocument();
  });

  it('renders search button', () => {
    renderPage();
    expect(screen.getByRole('button', { name: /search/i })).toBeInTheDocument();
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

    const searchInput = screen.getByPlaceholderText('Search arXiv or Semantic Scholar...');
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

    const searchInput = screen.getByPlaceholderText('Search arXiv or Semantic Scholar...');
    await user.type(searchInput, 'graph neural networks');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Semantic Scholar rate limit reached/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText('Search Result Paper')).not.toBeInTheDocument();
  });

  it('expands cross-paper chat when clicked', async () => {
    const user = userEvent.setup();
    renderPage();

    const expandBtn = screen.getByText('Ask across all papers');
    await user.click(expandBtn);

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

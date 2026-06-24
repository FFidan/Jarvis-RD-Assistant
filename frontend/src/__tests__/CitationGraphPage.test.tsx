import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { CitationGraphPage } from '@/pages/CitationGraphPage';
import { fetchCitationsFromS2 } from '@/lib/api';

// Mock cytoscape so jsdom doesn't choke on canvas
vi.mock('cytoscape', () => {
  const mockCy = {
    on: vi.fn(),
    destroy: vi.fn(),
  };
  return { default: vi.fn(() => mockCy) };
});

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  const papers = [
    { id: 1, title: 'Attention Is All You Need' },
    { id: 2, title: 'BERT: Pre-training of Deep Bidirectional Transformers' },
    { id: 3, title: 'GPT-4 Technical Report' },
  ];
  return {
    ...orig,
    fetchPapersBrief: vi.fn().mockResolvedValue(papers),
    searchPapersBrief: vi.fn().mockResolvedValue(papers),
    getCitationGraph: vi.fn().mockResolvedValue({
      nodes: [
        { id: 1, title: 'Attention Is All You Need', citation_count: 50000, published_date: '2017-06-01', is_stub: false },
        { id: 10, title: 'Some Referenced Paper', citation_count: 100, published_date: '2015-01-01', is_stub: true },
      ],
      edges: [
        { source: 1, target: 10, is_influential: true, context: null },
      ],
    }),
    fetchCitationsFromS2: vi.fn().mockResolvedValue({
      citations_added: 5, references_added: 3, stubs_created: 2,
    }),
  };
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CitationGraphPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('CitationGraphPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the page title', () => {
    renderPage();
    expect(screen.getByText('Citation Graph')).toBeInTheDocument();
  });

  it('shows paper selection section', () => {
    renderPage();
    expect(screen.getByText('Paper Selection')).toBeInTheDocument();
  });

  it('shows empty state when no papers selected', () => {
    renderPage();
    expect(screen.getByText('No citations loaded')).toBeInTheDocument();
    expect(
      screen.getByText("Select papers above and click 'Fetch Citations' to build the citation network."),
    ).toBeInTheDocument();
  });

  it('renders the depth slider', () => {
    renderPage();
    expect(screen.getByText(/Depth:/)).toBeInTheDocument();
  });

  it('renders Fetch Citations button', () => {
    renderPage();
    expect(screen.getByText('Fetch Citations')).toBeInTheDocument();
  });

  it('renders layout selector', () => {
    renderPage();
    expect(screen.getByText('Layout:')).toBeInTheDocument();
  });

  it('shows search input for papers', () => {
    renderPage();
    expect(screen.getByPlaceholderText('Search papers to add to citation graph...')).toBeInTheDocument();
  });

  it('shows papers in search dropdown when typing', async () => {
    const user = userEvent.setup();
    renderPage();
    const searchInput = screen.getByPlaceholderText('Search papers to add to citation graph...');
    await user.type(searchInput, 'Attention');
    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });
  });

  it('adds paper to selection when clicked in dropdown', async () => {
    const user = userEvent.setup();
    renderPage();
    const searchInput = screen.getByPlaceholderText('Search papers to add to citation graph...');
    await user.type(searchInput, 'Attention');
    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });
    await user.click(screen.getByText('Attention Is All You Need'));
    // The badge with the paper title should appear
    await waitFor(() => {
      expect(screen.getByText('1/10 papers selected')).toBeInTheDocument();
    });
  });

  it('disables Fetch Citations when no papers are selected', () => {
    renderPage();
    expect(screen.getByRole('button', { name: /Fetch Citations/i })).toBeDisabled();
  });

  it('fetches citations only for the selected papers', async () => {
    const user = userEvent.setup();
    renderPage();
    const searchInput = screen.getByPlaceholderText('Search papers to add to citation graph...');
    await user.type(searchInput, 'Attention');
    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });
    await user.click(screen.getByText('Attention Is All You Need'));
    await waitFor(() => {
      expect(screen.getByText('1/10 papers selected')).toBeInTheDocument();
    });

    const fetchButton = screen.getByRole('button', { name: /Fetch Citations/i });
    expect(fetchButton).toBeEnabled();
    await user.click(fetchButton);

    await waitFor(() => {
      expect(fetchCitationsFromS2).toHaveBeenCalledWith(1);
    });
    expect(fetchCitationsFromS2).toHaveBeenCalledTimes(1);
  });
});

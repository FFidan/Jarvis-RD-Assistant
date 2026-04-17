import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { PaperDetailPage } from '@/pages/PaperDetailPage';

// Track dismiss calls for banner tests
let mockPaperDetailNoteDismissed = false;
const mockSetPaperDetailNoteDismissed = vi.fn((value: boolean) => {
  mockPaperDetailNoteDismissed = value;
});

vi.mock('@/stores/ui-store', () => ({
  useUIStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      paperDetailNoteDismissed: mockPaperDetailNoteDismissed,
      setPaperDetailNoteDismissed: mockSetPaperDetailNoteDismissed,
      sidebarCollapsed: false,
      selectedPaperId: null,
      checklistDismissed: false,
      toggleSidebar: vi.fn(),
      setSelectedPaperId: vi.fn(),
      dismissChecklist: vi.fn(),
    }),
}));

// Mock the API module
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchPaperDetail: vi.fn(),
    fetchNotes: vi.fn(),
    fetchDecks: vi.fn(),
    upsertUserState: vi.fn(),
    createNote: vi.fn(),
    deleteNote: vi.fn(),
    downloadPdf: vi.fn(),
    processPdf: vi.fn(),
    summarizePaper: vi.fn(),
    generateCards: vi.fn(),
  };
});

// Mock the streaming chat to avoid SSE complexities
vi.mock('@/hooks/use-streaming-chat', () => ({
  useStreamingChat: () => ({
    messages: [],
    sources: [],
    isStreaming: false,
    sendMessage: vi.fn(),
    stopStreaming: vi.fn(),
    clearChat: vi.fn(),
  }),
}));

import { fetchPaperDetail, fetchNotes, fetchDecks } from '@/lib/api';
const mockFetchPaperDetail = vi.mocked(fetchPaperDetail);
const mockFetchNotes = vi.mocked(fetchNotes);
const mockFetchDecks = vi.mocked(fetchDecks);

const MOCK_PAPER = {
  id: 42,
  external_id: 'arxiv:2301.00001',
  source_type: 'arxiv' as const,
  title: 'Attention Is All You Need',
  authors: ['Vaswani, A.', 'Shazeer, N.', 'Parmar, N.'],
  abstract: 'The dominant sequence transduction models are based on...',
  published_date: '2017-06-12',
  url: 'https://arxiv.org/abs/1706.03762',
  pdf_url: 'https://arxiv.org/pdf/1706.03762',
  pdf_local_path: null,
  pdf_downloaded: false,
  citation_count: 95000,
  priority_score: 0.95,
  metadata: {},
  is_read: false,
  discovered_at: '2026-01-01T00:00:00Z',
  created_at: '2026-01-01T00:00:00Z',
};

const MOCK_SUMMARY = {
  id: 1,
  paper_id: 42,
  summary_brief: 'This paper proposes the Transformer architecture.',
  summary_detailed: 'A detailed description of the Transformer architecture.',
  tldr: 'Transformers replace recurrence with attention.',
  key_findings: [
    {
      finding: 'Self-attention is more efficient than recurrence',
      quote: 'Self-attention connects all positions with O(1) operations.',
      page_number: 3,
      chunk_id: 5,
      verified: true,
      snapshot_path: null,
    },
  ],
  methodology: 'Encoder-decoder architecture with multi-head attention.',
  limitations: 'Quadratic complexity with sequence length.',
  relevance_notes: null,
  confidence: 'HIGH' as const,
  cross_references: [
    {
      related_paper_id: 10,
      relationship: 'extends',
      explanation: 'Builds on sequence-to-sequence learning.',
      related_quote: null,
    },
  ],
  llm_model: 'mistral-nemo',
  summary_verified: false,
  created_at: '2026-01-01T00:00:00Z',
};

const MOCK_CHUNKS = [
  {
    id: 1,
    paper_id: 42,
    chunk_index: 0,
    content: 'The Transformer model architecture...',
    page_number: 1,
    start_char: 0,
    end_char: 500,
    embedding_id: 'abc',
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 2,
    paper_id: 42,
    chunk_index: 1,
    content: 'Multi-head attention allows the model...',
    page_number: 2,
    start_char: 500,
    end_char: 1000,
    embedding_id: 'def',
    created_at: '2026-01-01T00:00:00Z',
  },
];

const MOCK_USER_STATE = {
  status: 'reading',
  rating: 4,
  user_notes: 'Great paper on attention.',
  flagged: false,
};

const MOCK_NOTES = [
  {
    id: 1,
    paper_id: 42,
    user_note: 'Key insight about positional encoding.',
    highlight_text: 'sinusoidal positional encoding',
    page_number: 5,
    created_at: '2026-01-15T10:30:00Z',
  },
];

function renderPage(paperId = '42') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/paper/${paperId}`]}>
        <Routes>
          <Route path="paper/:paperId" element={<PaperDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('PaperDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPaperDetailNoteDismissed = false;
    mockFetchDecks.mockResolvedValue([]);
    mockFetchNotes.mockResolvedValue(MOCK_NOTES);
  });

  it('renders paper title and author info after loading', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: MOCK_USER_STATE,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });
    expect(screen.getByText('Vaswani, A., Shazeer, N., Parmar, N.')).toBeInTheDocument();
    expect(screen.getByText('arxiv')).toBeInTheDocument();
    expect(screen.getByText('95000 citations')).toBeInTheDocument();
  });

  it('shows all tab triggers', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'Summary' })).toBeInTheDocument();
    });
    expect(screen.getByRole('tab', { name: 'Evidence' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Chunks' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Cross-References' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Notes' })).toBeInTheDocument();
  });

  it('switches between tabs', async () => {
    const user = userEvent.setup();
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    // Default tab is Summary
    await waitFor(() => {
      expect(screen.getByText('This paper proposes the Transformer architecture.')).toBeInTheDocument();
    });

    // Switch to Evidence tab
    await user.click(screen.getByRole('tab', { name: 'Evidence' }));
    await waitFor(() => {
      expect(screen.getByText('Verified Findings')).toBeInTheDocument();
    });

    // Switch to Chunks tab
    await user.click(screen.getByRole('tab', { name: 'Chunks' }));
    await waitFor(() => {
      expect(screen.getByText('2 chunks extracted')).toBeInTheDocument();
    });
  });

  it('displays summary content in the Summary tab', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('This paper proposes the Transformer architecture.')).toBeInTheDocument();
    });
    expect(screen.getByText('A detailed description of the Transformer architecture.')).toBeInTheDocument();
    expect(screen.getByText('Encoder-decoder architecture with multi-head attention.')).toBeInTheDocument();
    expect(screen.getByText('Quadratic complexity with sequence length.')).toBeInTheDocument();
    expect(screen.getByText('Self-attention is more efficient than recurrence')).toBeInTheDocument();
  });

  it('shows notes tab with existing notes', async () => {
    const user = userEvent.setup();
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });

    // Switch to Notes tab
    await user.click(screen.getByRole('tab', { name: 'Notes' }));
    await waitFor(() => {
      expect(screen.getByText('Key insight about positional encoding.')).toBeInTheDocument();
    });
  });

  it('renders user state form with initial values', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: MOCK_USER_STATE,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('My Notes')).toBeInTheDocument();
    });
    expect(screen.getByText('Rating: 4')).toBeInTheDocument();
    expect(screen.getByText('Save Notes')).toBeInTheDocument();
  });

  it('renders RAG chat section with scope toggle', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Ask about this paper')).toBeInTheDocument();
    });
    expect(screen.getByText('This paper')).toBeInTheDocument();
    expect(screen.getByText('All papers')).toBeInTheDocument();
  });

  it('shows error state for invalid paper ID', () => {
    renderPage('abc');

    expect(screen.getByText(/Invalid paper ID/)).toBeInTheDocument();
  });

  it('shows no-summary empty state when summary is null', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: null,
      chunks: [],
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('No summary available')).toBeInTheDocument();
    });
  });

  it('shows workspace note banner when paperDetailNoteDismissed is false', async () => {
    mockPaperDetailNoteDismissed = false;
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: null,
      chunks: [],
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });

    expect(screen.getByText(/Paper Detail is the workspace/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument();
  });

  it('hides workspace note banner when × is clicked', async () => {
    const user = userEvent.setup();
    mockPaperDetailNoteDismissed = false;
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: null,
      chunks: [],
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(mockSetPaperDetailNoteDismissed).toHaveBeenCalledWith(true);
  });

  it('does not show workspace note banner when paperDetailNoteDismissed is true', async () => {
    mockPaperDetailNoteDismissed = true;
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: null,
      chunks: [],
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });

    expect(screen.queryByText(/Paper Detail is the workspace/)).not.toBeInTheDocument();
  });
});

/**
 * PaperDetailPage.test.tsx
 *
 * Regression-guard for the 3-pane Paper Detail layout.
 *
 * The old tab-based tests have been migrated to match the new scrolling
 * research-log layout (F2 IA redesign). All existing data (summary, evidence,
 * chunks, cross-refs, notes, contradictions) must still surface correctly;
 * they are now always rendered in the scrolling column rather than behind
 * tab clicks.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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
    fetchContradictions: vi.fn(),
    scanPaperContradictions: vi.fn(),
    fetchNotes: vi.fn(),
    fetchDecks: vi.fn(),
    zoteroGetLinkage: vi.fn(),
    zoteroPushPaper: vi.fn(),
    zoteroResync: vi.fn(),
    zoteroSyncAnnotations: vi.fn(),
    promoteZoteroNote: vi.fn(),
    upsertAnnotations: vi.fn(),
    createNote: vi.fn(),
    deleteNote: vi.fn(),
    downloadPdf: vi.fn(),
    processPdf: vi.fn(),
    summarizePaper: vi.fn(),
  };
});

// Mock the streaming chat to avoid SSE complexities
vi.mock('@/hooks/use-streaming-chat', () => ({
  useStreamingChat: () => ({
    messages: [],
    sources: [],
    isStreaming: false,
    phase: 'idle',
    sendMessage: vi.fn(),
    stopStreaming: vi.fn(),
    clearChat: vi.fn(),
    modelUsed: null,
  }),
}));

import { fetchPaperDetail, fetchContradictions, fetchNotes, fetchDecks, zoteroGetLinkage } from '@/lib/api';
const mockFetchPaperDetail = vi.mocked(fetchPaperDetail);
const mockFetchContradictions = vi.mocked(fetchContradictions);
const mockFetchNotes = vi.mocked(fetchNotes);
const mockFetchDecks = vi.mocked(fetchDecks);
const mockZoteroGetLinkage = vi.mocked(zoteroGetLinkage);

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
  state: 'reading' as const,
  state_before_trash: null,
  starred: true,
  rating: 4,
  user_notes: 'Great paper on attention.',
  flagged: false,
  updated_at: '2026-04-30T00:00:00Z',
};

const MOCK_NOTES = [
  {
    id: 1,
    paper_id: 42,
    user_note: 'Key insight about positional encoding.',
    highlight_text: 'sinusoidal positional encoding',
    page_number: 5,
    source: 'user' as const,
    zotero_annotation_key: null,
    verification_status: 'unverified' as const,
    verified_quote: null,
    verified_page_number: null,
    promoted_at: null,
    created_at: '2026-01-15T10:30:00Z',
  },
];

function renderPage(paperId = '42', search = '') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/paper/${paperId}${search}`]}>
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
    mockFetchContradictions.mockResolvedValue({ contradictions: [], total: 0 });
    mockFetchNotes.mockImplementation((_paperId, source) =>
      Promise.resolve(source === 'zotero' ? [] : MOCK_NOTES),
    );
    mockZoteroGetLinkage.mockResolvedValue({
      zotero_item_key: null,
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    });
  });

  it('renders paper title and author info after loading', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: MOCK_USER_STATE,
    });

    renderPage();

    // Title appears in both h1 and breadcrumb; check for h1 specifically
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
    });
    expect(screen.getByText('Vaswani, A., Shazeer, N., Parmar, N.')).toBeInTheDocument();
    expect(screen.getByText('arxiv')).toBeInTheDocument();
    expect(screen.getByText('95000 citations')).toBeInTheDocument();
  });

  // ── Section navigation (replaces old tab tests) ───────────────────────────

  it('shows all §-section headings in the scrolling column', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
    });

    // All section headings visible in the scrolling column (no tab click needed)
    expect(document.getElementById('section-brief')).toBeInTheDocument();
    expect(document.getElementById('section-detailed')).toBeInTheDocument();
    expect(document.getElementById('section-methodology')).toBeInTheDocument();
    expect(document.getElementById('section-limitations')).toBeInTheDocument();
    expect(document.getElementById('section-findings')).toBeInTheDocument();
    expect(document.getElementById('section-crossrefs')).toBeInTheDocument();
    expect(document.getElementById('section-notes')).toBeInTheDocument();
    expect(document.getElementById('section-chunks')).toBeInTheDocument();
    expect(document.getElementById('section-ask')).toBeInTheDocument();
  });

  it('displays summary content without any tab click required', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    // All summary content visible immediately (no tab interaction).
    // PaperResearchLog inlines brief/detailed/methodology/limitations and
    // EvidenceTab renders findings; some text appears in >1 place so use
    // getAllByText.
    await waitFor(() => {
      expect(screen.getAllByText('This paper proposes the Transformer architecture.').length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText('A detailed description of the Transformer architecture.').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Encoder-decoder architecture with multi-head attention.').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Quadratic complexity with sequence length.').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Self-attention is more efficient than recurrence').length).toBeGreaterThan(0);
  });

  it('displays cross-reference data', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Builds on sequence-to-sequence learning.')).toBeInTheDocument();
    });
  });

  it('renders TL;DR in the header band', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Transformers replace recurrence with attention.')).toBeInTheDocument();
    });
  });

  it('chunks section: shows expand toggle (lazy default)', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
    });

    // Chunks are collapsed by default — content not visible
    expect(screen.queryByText('The Transformer model architecture...')).not.toBeInTheDocument();
    // Toggle button is present
    expect(screen.getByTestId('chunks-expand-toggle')).toBeInTheDocument();
  });

  it('chunks expand when toggle is clicked — shows chunk count line', async () => {
    const user = userEvent.setup();
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('chunks-expand-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('chunks-expand-toggle'));

    // After expanding LazyChunksSection, ChunksTab renders "N chunks extracted"
    await waitFor(() => {
      expect(screen.getByText(/2 chunks extracted/)).toBeInTheDocument();
    });
    // Individual chunk buttons are visible
    expect(screen.getByText(/Chunk 0/)).toBeInTheDocument();
    expect(screen.getByText(/Chunk 1/)).toBeInTheDocument();
  });

  it('shows notes in the scrolling column', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    // Notes load via their own query; wait for them
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
      expect(screen.getByText('Quick Rating')).toBeInTheDocument();
    });
    expect(screen.getByText('Rating: 4')).toBeInTheDocument();
    expect(screen.getByText('Save Rating')).toBeInTheDocument();
  });

  it('renders RAG chat section', async () => {
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

  it('renders the Zotero panel disabled when the paper has no project links', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
      has_project_links: false,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Zotero')).toBeInTheDocument();
    });
    expect(screen.getByText('Link to a project first to enable Zotero push.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send to Zotero' })).toBeDisabled();
  });

  it('renders the Zotero panel enabled when the paper has project links', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
      has_project_links: true,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Send to Zotero' })).toBeInTheDocument();
    });
    expect(screen.queryByText('Link to a project first to enable Zotero push.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send to Zotero' })).toBeEnabled();
  });

  it('shows a degraded Zotero state when linkage status fails to load', async () => {
    mockZoteroGetLinkage.mockRejectedValueOnce(new Error('failed to load linkage'));
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
      has_project_links: true,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Zotero status unavailable.')).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: 'Send to Zotero' })).not.toBeInTheDocument();
  });

  it('shows verified contradictions in the paper sidebar', async () => {
    // Both the page count query and ContradictionsPanel call fetchContradictions;
    // use mockResolvedValue (not Once) so both calls return the contradiction.
    mockFetchContradictions.mockResolvedValue({
      total: 1,
      contradictions: [
        {
          id: 7,
          paper_a_id: 42,
          paper_b_id: 99,
          paper_a_title: 'Attention Is All You Need',
          paper_b_title: 'Recurrence Still Matters',
          finding_a: 'Self-attention removes recurrence.',
          finding_b: 'Recurrence is required for sequence modelling.',
          quote_a: 'We dispense with recurrence.',
          quote_b: 'Recurrence is required.',
          page_a: 2,
          page_b: 4,
          contradiction_type: 'methodological',
          explanation: 'The papers disagree about recurrence requirements.',
          confidence: 0.87,
          status: 'verified',
          created_at: '2026-04-25T12:00:00Z',
        },
      ],
    });
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
      has_project_links: true,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Recurrence Still Matters')).toBeInTheDocument();
    });
    expect(mockFetchContradictions).toHaveBeenCalledWith({
      paper_id: 42,
      status: 'verified',
      limit: 20,
    });
    expect(screen.getByText(/The papers disagree about recurrence requirements/)).toBeInTheDocument();
    expect(screen.getByText(/We dispense with recurrence/)).toBeInTheDocument();
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
      expect(screen.getByText(/Run Analyze to generate a summary/)).toBeInTheDocument();
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
      expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
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
      expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
    });

    expect(screen.queryByText(/Paper Detail is the workspace/)).not.toBeInTheDocument();
  });

  // ── Left rail (TOC + pipeline) ─────────────────────────────────────────────

  it('renders the left-rail TOC with section labels', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: MOCK_PAPER,
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: MOCK_USER_STATE,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
    });

    // TOC navigation labels should appear in nav
    const nav = screen.getByRole('navigation', { name: 'Paper navigation' });
    expect(nav).toBeInTheDocument();
    expect(nav).toHaveTextContent('Brief');
    expect(nav).toHaveTextContent('Methodology');
  });

  it('renders pipeline status in the TOC', async () => {
    mockFetchPaperDetail.mockResolvedValue({
      paper: { ...MOCK_PAPER, pdf_downloaded: true },
      summary: MOCK_SUMMARY,
      chunks: MOCK_CHUNKS,
      user_state: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
    });

    // Pipeline section visible in TOC
    expect(screen.getByText('§ Pipeline')).toBeInTheDocument();
    expect(screen.getByText('Downloaded')).toBeInTheDocument();
    expect(screen.getByText('Summarized')).toBeInTheDocument();
  });

  describe('?action=process scroll behaviour', () => {
    // jsdom does not implement scrollIntoView — define it before spying
    beforeEach(() => {
      if (!Element.prototype.scrollIntoView) {
        Element.prototype.scrollIntoView = () => {};
      }
    });

    it('does not call scrollIntoView while data is still loading', async () => {
      const scrollIntoViewSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
      // fetchPaperDetail never resolves during this test
      mockFetchPaperDetail.mockReturnValue(new Promise(() => {}));

      renderPage('42', '?action=process');

      // Give any synchronous effects a chance to fire
      await new Promise((r) => setTimeout(r, 50));

      expect(scrollIntoViewSpy).not.toHaveBeenCalled();

      scrollIntoViewSpy.mockRestore();
    });

    it('calls scrollIntoView once data has loaded', async () => {
      const scrollIntoViewSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

      // Set up a DOM node that matches the scroll target id
      const el = document.createElement('button');
      el.id = 'paper-action-process';
      document.body.appendChild(el);

      mockFetchPaperDetail.mockResolvedValue({
        paper: MOCK_PAPER,
        summary: MOCK_SUMMARY,
        chunks: MOCK_CHUNKS,
        user_state: MOCK_USER_STATE,
      });

      renderPage('42', '?action=process');

      await waitFor(() => {
        expect(screen.getByRole('heading', { level: 1, name: 'Attention Is All You Need' })).toBeInTheDocument();
      });

      expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1);
      expect(scrollIntoViewSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' });

      document.body.removeChild(el);
      scrollIntoViewSpy.mockRestore();
    });
  });
});

/**
 * PaperResearchLog.test.tsx
 *
 * Tests that:
 * - Every §-section is rendered with the correct anchor id.
 * - Existing data (summary, evidence, cross-refs, notes, chunks) surfaces correctly.
 * - Chunks are lazy/collapsed by default and expand on toggle.
 * - recommendation_score renders only when present (no fabrication).
 * - Breadcrumb reflects lifecycle state.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PaperResearchLog } from '@/components/paper/PaperResearchLog';
import type { Paper, Summary, Chunk, UserState } from '@/types';

// ── Mocks ──────────────────────────────────────────────────────────────────

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

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchNotes: vi.fn().mockResolvedValue([]),
    fetchDecks: vi.fn().mockResolvedValue([]),
    zoteroGetLinkage: vi.fn().mockResolvedValue({
      zotero_item_key: null,
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    }),
  };
});

// ── Fixtures ───────────────────────────────────────────────────────────────

const PAPER: Paper = {
  id: 1,
  external_id: 'arxiv:2301.00001',
  source_type: 'arxiv',
  title: 'Attention Is All You Need',
  authors: ['Vaswani, A.', 'Shazeer, N.'],
  abstract: null,
  published_date: '2017-06-12',
  url: 'https://arxiv.org/abs/1706.03762',
  pdf_url: null,
  pdf_local_path: null,
  pdf_downloaded: true,
  citation_count: 95000,
  priority_score: 0.95,
  metadata: {},
  discovered_at: '2024-01-01T00:00:00Z',
  created_at: '2024-01-01T00:00:00Z',
};

const SUMMARY: Summary = {
  id: 1,
  paper_id: 1,
  summary_brief: 'Brief: Transformer replaces RNNs with attention.',
  summary_detailed: 'Detailed: Full Transformer description here.',
  tldr: 'TL;DR: Transformers are better.',
  key_findings: [
    {
      finding: 'Key finding: Self-attention is O(1).',
      quote: 'Self-attention connects positions with O(1) operations.',
      page_number: 3,
      chunk_id: 5,
      verified: true,
      snapshot_path: null,
    },
  ],
  methodology: 'Methodology: Encoder-decoder with multi-head attention.',
  limitations: 'Limitations: Quadratic complexity.',
  relevance_notes: null,
  confidence: 'HIGH',
  cross_references: [
    {
      related_paper_id: 10,
      relationship: 'extends',
      explanation: 'Builds on seq2seq.',
      related_quote: null,
    },
  ],
  llm_model: 'test-model',
  summary_verified: true,
  created_at: '2024-01-01T00:00:00Z',
};

const CHUNKS: Chunk[] = [
  {
    id: 1,
    paper_id: 1,
    chunk_index: 0,
    content: 'First chunk content here.',
    page_number: 1,
    start_char: 0,
    end_char: 100,
    embedding_id: null,
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 2,
    paper_id: 1,
    chunk_index: 1,
    content: 'Second chunk content here.',
    page_number: 2,
    start_char: 100,
    end_char: 200,
    embedding_id: null,
    created_at: '2024-01-01T00:00:00Z',
  },
];

const USER_STATE: UserState = {
  state: 'reading',
  state_before_trash: null,
  starred: false,
  rating: null,
  user_notes: null,
  flagged: false,
  updated_at: '2024-01-01T00:00:00Z',
};

function renderLog(
  overrides: Partial<{
    summary: Summary | null;
    chunks: Chunk[];
    userState: UserState | null;
    recommendationScore: number | null;
  }> = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <PaperResearchLog
          paper={PAPER}
          summary={overrides.summary !== undefined ? overrides.summary : SUMMARY}
          chunks={overrides.chunks ?? CHUNKS}
          userState={overrides.userState !== undefined ? overrides.userState : USER_STATE}
          paperId={1}
          evidenceCount={1}
          crossRefCount={1}
          contradictionCount={0}
          noteCount={0}
          recommendationScore={overrides.recommendationScore ?? null}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

// ── Tests ──────────────────────────────────────────────────────────────────

describe('PaperResearchLog — section anchors', () => {
  it('renders all required section ids', () => {
    renderLog();
    const requiredIds = [
      'section-brief',
      'section-detailed',
      'section-methodology',
      'section-limitations',
      'section-findings',
      'section-crossrefs',
      'section-contradictions',
      'section-notes',
      'section-chunks',
      'section-ask',
    ];
    requiredIds.forEach((id) => {
      const el = document.getElementById(id);
      expect(el, `Section #${id} should exist in the DOM`).not.toBeNull();
    });
  });
});

describe('PaperResearchLog — breadcrumb', () => {
  it('shows Library / state / title in breadcrumb', () => {
    renderLog();
    expect(screen.getByText('Library')).toBeInTheDocument();
    expect(screen.getByText('reading')).toBeInTheDocument();
    // Title is in the breadcrumb + also in h1; at least one instance
    expect(screen.getAllByText('Attention Is All You Need').length).toBeGreaterThanOrEqual(1);
  });

  it('does NOT render recommendation_score when null', () => {
    renderLog({ recommendationScore: null });
    expect(screen.queryByText(/Score/)).not.toBeInTheDocument();
  });

  it('renders recommendation_score badge when provided', () => {
    renderLog({ recommendationScore: 0.87 });
    expect(screen.getByText(/Score 87/)).toBeInTheDocument();
  });
});

describe('PaperResearchLog — summary sections', () => {
  it('renders brief summary text', () => {
    renderLog();
    expect(screen.getByText(/Brief: Transformer replaces RNNs with attention/)).toBeInTheDocument();
  });

  it('renders detailed summary text', () => {
    renderLog();
    expect(screen.getByText(/Detailed: Full Transformer description here/)).toBeInTheDocument();
  });

  it('renders TL;DR in the header band', () => {
    renderLog();
    expect(screen.getByText(/TL;DR: Transformers are better/)).toBeInTheDocument();
  });

  it('renders methodology text', () => {
    renderLog();
    expect(screen.getByText(/Methodology: Encoder-decoder with multi-head attention/)).toBeInTheDocument();
  });

  it('renders limitations text', () => {
    renderLog();
    expect(screen.getByText(/Limitations: Quadratic complexity/)).toBeInTheDocument();
  });

  it('renders key finding text', () => {
    renderLog();
    expect(screen.getByText(/Key finding: Self-attention is O\(1\)/)).toBeInTheDocument();
  });

  it('shows "No summary available" placeholder when summary is null', () => {
    renderLog({ summary: null });
    expect(screen.getByText(/Run Analyze to generate a summary/)).toBeInTheDocument();
  });

  it('shows summary_verified chip when verified', () => {
    renderLog({ summary: { ...SUMMARY, summary_verified: true } });
    expect(screen.getByText(/Summary verified against source PDF/)).toBeInTheDocument();
  });

  it('shows unverified warning when not verified', () => {
    renderLog({ summary: { ...SUMMARY, summary_verified: false } });
    expect(screen.getByText(/LLM-generated/)).toBeInTheDocument();
  });
});

describe('PaperResearchLog — coverage transparency', () => {
  it('shows "full paper covered" note when passes > 1 and coverage is clean', () => {
    renderLog({ summary: { ...SUMMARY, coverage: 1.0, passes: 3 } });
    expect(screen.getByTestId('coverage-note')).toBeInTheDocument();
    expect(screen.getByText(/Read in 3 passes — full paper covered/)).toBeInTheDocument();
  });

  it('shows abstract-fallback warning when coverage is 0', () => {
    renderLog({ summary: { ...SUMMARY, coverage: 0, passes: 1 } });
    const banner = screen.getByTestId('coverage-warning');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent(/abstract-based fallback/);
  });

  it('shows percentage warning when 0 < coverage < 1', () => {
    renderLog({ summary: { ...SUMMARY, coverage: 0.6, passes: 2 } });
    const banner = screen.getByTestId('coverage-warning');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent(/first ~60%/);
  });

  it('shows no coverage note or warning for single-pass short paper', () => {
    renderLog({ summary: { ...SUMMARY, coverage: undefined, passes: undefined } });
    expect(screen.queryByTestId('coverage-note')).not.toBeInTheDocument();
    expect(screen.queryByTestId('coverage-warning')).not.toBeInTheDocument();
  });
});

describe('PaperResearchLog — cross-references', () => {
  it('renders cross-reference data', () => {
    renderLog();
    expect(screen.getByText('Builds on seq2seq.')).toBeInTheDocument();
  });
});

describe('PaperResearchLog — chunks (lazy)', () => {
  it('does NOT show chunk content before expand', () => {
    renderLog();
    expect(screen.queryByText('First chunk content here.')).not.toBeInTheDocument();
    expect(screen.queryByText('Second chunk content here.')).not.toBeInTheDocument();
  });

  it('shows chunk count in expand toggle button', () => {
    renderLog();
    expect(screen.getByTestId('chunks-expand-toggle')).toHaveTextContent('Show 2 passages');
  });

  it('expands chunks when toggle is clicked', async () => {
    const user = userEvent.setup();
    renderLog();
    const toggle = screen.getByTestId('chunks-expand-toggle');
    await user.click(toggle);
    // After expanding LazyChunksSection, ChunksTab renders "N chunks extracted"
    await waitFor(() => {
      expect(screen.getByText(/2 passages from the PDF/)).toBeInTheDocument();
    });
    // Individual chunk header buttons visible
    expect(screen.getByText(/Chunk 0/)).toBeInTheDocument();
    expect(screen.getByText(/Chunk 1/)).toBeInTheDocument();
  });

  it('collapses chunks when toggle is clicked a second time', async () => {
    const user = userEvent.setup();
    renderLog();
    const toggle = screen.getByTestId('chunks-expand-toggle');
    await user.click(toggle); // expand
    await waitFor(() => {
      expect(screen.getByText(/2 passages from the PDF/)).toBeInTheDocument();
    });
    await user.click(toggle); // collapse
    await waitFor(() => {
      expect(screen.queryByText(/2 passages from the PDF/)).not.toBeInTheDocument();
    });
  });

  it('shows analyze prompt when chunks array is empty', () => {
    renderLog({ chunks: [] });
    expect(screen.getByText(/Analyze this paper to enable search and Q&A/)).toBeInTheDocument();
  });
});

describe('PaperResearchLog — paper header', () => {
  it('renders paper title as h1', () => {
    renderLog();
    const h1 = screen.getByRole('heading', { level: 1 });
    expect(h1).toHaveTextContent('Attention Is All You Need');
  });

  it('renders authors', () => {
    renderLog();
    expect(screen.getByText(/Vaswani, A./)).toBeInTheDocument();
  });

  it('renders citation count', () => {
    renderLog();
    expect(screen.getByText(/95000 citations/)).toBeInTheDocument();
  });

  it('renders external link when url is valid', () => {
    renderLog();
    const link = screen.getByRole('link', { name: /Open original/ });
    expect(link).toHaveAttribute('href', PAPER.url);
  });
});

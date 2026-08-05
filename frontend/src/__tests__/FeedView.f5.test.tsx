/**
 * FeedView — F5 regression: Source facet must filter on Library/Trash surfaces.
 *
 * Coverage:
 *  - Library: fetchFeed receives sourceTypes when the prop is set
 *  - Trash: fetchFeed receives sourceTypes when the prop is set
 *  - Library: pagination resets to offset=0 when sourceTypes changes
 *  - Inbox: remains byte-identical (sourceTypes already passed through)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { FeedView } from '@/components/feed/FeedView';
import type { FeedPaper } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
import { makeFeedPaper } from '@/__tests__/fixtures/feed-paper';

// Hoisted so the same field values are visible both to test bodies and to the
// hoisted vi.mock factory below.
const sourceFacetPaperOverrides = vi.hoisted(() => ({
  external_id: 'arxiv:2601.00001',
  title: 'Source Facet Test Paper',
  authors: ['Alice'],
  abstract: 'Abstract.',
  url: 'https://example.com/paper/1',
  discovered_at: null,
  priority_score: 0.5,
  created_at: '2026-01-02T00:00:00Z',
  summary_brief: 'Brief',
  tldr: null,
  confidence: null,
  has_chunks: false,
  has_summary: false,
  state: 'to_read' as const,
  user_state: {
    state: 'to_read' as const,
    state_before_trash: null,
    starred: false,
    rating: null,
    user_notes: null,
    flagged: false,
    updated_at: null,
  },
}));

const PAPER: FeedPaper = makeFeedPaper(sourceFacetPaperOverrides);

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  const { makeFeedPaper: makePaper } = await import('@/__tests__/fixtures/feed-paper');
  const paper = makePaper(sourceFacetPaperOverrides);
  return createApiMock({
    fetchFeed: async () => ({ papers: [paper], total: 1 }),
    savePaper: async () => ({ ok: true }),
    skipPaper: async () => ({ status: 'ok', paper_id: 1 }),
    markReading: async () => ({ status: 'ok', paper_id: 1 }),
    markDone: async () => ({ status: 'ok', paper_id: 1 }),
    trashPaper: async () => ({ status: 'ok', paper_id: 1 }),
    restorePaper: async () => ({ ok: true }),
    hardDeletePaper: async () => ({ deleted: 1 }),
    starPaper: async () => ({ status: 'ok', paper_id: 1 }),
    unstarPaper: async () => ({ status: 'ok', paper_id: 1 }),
    bulkAction: async () => ({ succeeded: [], failed: [] }),
  });
});

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

function makeQC() {
  return createTestQueryClient();
}

/** Renders FeedView inside a MemoryRouter; initialUrl controls URL search params. */
function renderFeedView(
  surface: Parameters<typeof FeedView>[0]['surface'],
  props: Partial<Parameters<typeof FeedView>[0]> = {},
  initialUrl = '/',
) {
  const qc = makeQC();
  const result = renderWithProviders(
    <MemoryRouter initialEntries={[initialUrl]}>
      <Routes>
        <Route path="/" element={<FeedView surface={surface} {...props} />} />
      </Routes>
    </MemoryRouter>,
    { queryClient: qc },
  );
  return { ...result, qc };
}

describe('FeedView — F5: sourceTypes forwarded to fetchFeed on Library/Trash', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('library: passes sourceTypes to fetchFeed when the prop is set', async () => {
    const api = await import('@/lib/api');

    renderFeedView('library', { sourceTypes: 'arxiv' });

    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ sourceTypes: 'arxiv' }),
      ),
    );
  });

  it('trash: passes sourceTypes to fetchFeed when the prop is set', async () => {
    const api = await import('@/lib/api');
    const trashPaper: FeedPaper = { ...PAPER, state: 'trash' };
    vi.mocked(api.fetchFeed).mockResolvedValueOnce({ papers: [trashPaper], total: 1 });

    renderFeedView('trash', { sourceTypes: 'semantic_scholar' });

    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ sourceTypes: 'semantic_scholar' }),
      ),
    );
  });

  it('inbox: still passes sourceTypes to fetchFeed (unchanged)', async () => {
    const api = await import('@/lib/api');
    const inboxPaper: FeedPaper = { ...PAPER, state: 'inbox' };
    vi.mocked(api.fetchFeed).mockResolvedValueOnce({ papers: [inboxPaper], total: 1 });

    renderFeedView('inbox', { sourceTypes: 'arxiv' });

    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ sourceTypes: 'arxiv' }),
      ),
    );
  });
});

describe('FeedView — F5: pagination reset on sourceTypes change (Library)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('resets offset to 0 when sourceTypes changes while paginated', async () => {
    const api = await import('@/lib/api');
    // Start with offset=30 (second page); we verify the reset via fetchFeed call args.
    const initialUrl = '/?offset=30&limit=30';

    const qc = makeQC();
    const { rerender } = renderWithProviders(
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          <Route
            path="/"
            element={<FeedView surface="library" sourceTypes={null} />}
          />
        </Routes>
      </MemoryRouter>,
      { queryClient: qc },
    );

    // Wait for initial fetch at offset=30 with no sourceTypes
    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());

    vi.mocked(api.fetchFeed).mockClear();
    vi.mocked(api.fetchFeed).mockResolvedValue({ papers: [], total: 0 });

    // Re-render with sourceTypes='arxiv' — should trigger offset reset to 0
    rerender(
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          <Route
            path="/"
            element={<FeedView surface="library" sourceTypes="arxiv" />}
          />
        </Routes>
      </MemoryRouter>,
    );

    // After reset, fetchFeed should be called with offset=0
    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ offset: 0, sourceTypes: 'arxiv' }),
      ),
    );
  });

  it('shows empty state when no papers match the source filter', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockResolvedValue({ papers: [], total: 0 });

    renderFeedView('library', { sourceTypes: 'semantic_scholar' });

    await waitFor(() =>
      screen.getByText(/no papers in your library/i),
    );
  });
});

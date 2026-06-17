import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { toast } from 'sonner';
import { FeedView } from '@/components/feed/FeedView';
import type { FeedPaper } from '@/types';

// Minimal FeedPaper fixture used across all surface tests (test-body only — not used inside vi.mock factory)
const mockPaper: FeedPaper = {
  id: 1,
  external_id: 'arxiv:2601.00001',
  source_type: 'arxiv',
  title: 'Surface Callback Test Paper',
  authors: ['Alice Researcher'],
  abstract: 'A paper for testing surface-aware callbacks.',
  published_date: '2026-01-01',
  url: 'https://example.com/paper/1',
  pdf_url: null,
  pdf_local_path: null,
  pdf_downloaded: false,
  discovered_at: null,
  citation_count: 0,
  priority_score: 0.5,
  metadata: {},
  created_at: '2026-01-02T00:00:00Z',
  summary_brief: 'Brief summary',
  tldr: null,
  confidence: null,
  rating: null,
  has_chunks: false,
  has_summary: false,
  state: 'inbox',
  state_before_trash: null,
  starred: false,
  discovery_origin: 'pulse',
  user_state: {
    state: 'inbox',
    state_before_trash: null,
    starred: false,
    rating: null,
    user_notes: null,
    flagged: false,
    updated_at: null,
  },
  recent_feedback: null,
};

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  // Inline paper object — cannot reference module-level vars here (vi.mock is hoisted)
  const paper = {
    id: 1,
    external_id: 'arxiv:2601.00001',
    source_type: 'arxiv' as const,
    title: 'Surface Callback Test Paper',
    authors: ['Alice Researcher'],
    abstract: 'A paper for testing surface-aware callbacks.',
    published_date: '2026-01-01',
    url: 'https://example.com/paper/1',
    pdf_url: null,
    citation_count: 0,
    priority_score: 0.5,
    metadata: {},
    created_at: '2026-01-02T00:00:00Z',
    summary_brief: 'Brief summary',
    tldr: null,
    confidence: null,
    rating: null,
    has_chunks: false,
    has_summary: false,
    state: 'inbox' as const,
    state_before_trash: null,
    starred: false,
    discovery_origin: 'pulse' as const,
    user_state: {
      state: 'inbox' as const,
      state_before_trash: null,
      starred: false,
      rating: null,
      user_notes: null,
      flagged: false,
      updated_at: null,
    },
    recent_feedback: null,
  };
  return {
    ...actual,
    fetchFeed: vi.fn().mockResolvedValue({ papers: [paper], total: 1 }),
    savePaper: vi.fn().mockResolvedValue({ ok: true }),
    skipPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    markReading: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    markDone: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    trashPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    restorePaper: vi.fn().mockResolvedValue({ ok: true }),
    hardDeletePaper: vi.fn().mockResolvedValue({ deleted: 1 }),
    starPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    unstarPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 1 }),
    bulkAction: vi.fn().mockResolvedValue({ succeeded: [], failed: [] }),
  };
});

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderFeedView(surface: Parameters<typeof FeedView>[0]['surface']) {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter>
        <FeedView surface={surface} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('FeedView — state-switch button rendering', () => {
  it('surface="inbox" renders a Save button for inbox-state paper', async () => {
    renderFeedView('inbox');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /save surface callback test paper/i })).toBeInTheDocument(),
    );
  });

  it('surface="library" renders lifecycle buttons for non-inbox paper', async () => {
    // FeedView wires all lifecycle callbacks regardless of surface
    renderFeedView('library');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /save surface callback test paper/i })).toBeInTheDocument(),
    );
  });

  it('surface="trash" renders Restore and Permanently-delete buttons for trash-state paper', async () => {
    const api = await import('@/lib/api');
    const trashPaper: FeedPaper = { ...mockPaper, state: 'trash' };
    vi.mocked(api.fetchFeed).mockResolvedValueOnce({ papers: [trashPaper], total: 1 });

    renderFeedView('trash');
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /restore surface callback test paper/i }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: /permanently delete surface callback test paper/i }),
      ).toBeInTheDocument();
    });
  });
});

describe('FeedView — Untagged facet', () => {
  function renderUntagged(untagged: boolean) {
    return render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <FeedView surface="inbox" untagged={untagged} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('passes untagged=true to fetchFeed when the untagged prop is set', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();

    renderUntagged(true);

    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ untagged: true }),
      ),
    );
  });

  it('does not pass untagged=true when the prop is false', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();

    renderUntagged(false);

    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());
    for (const [params] of vi.mocked(api.fetchFeed).mock.calls) {
      expect((params as { untagged?: boolean }).untagged).toBeFalsy();
    }
  });
});

describe('FeedView — mutation onError toasts (NI-3)', () => {
  it('shows a toast.error when savePaper fails on the inbox surface', async () => {
    const user = userEvent.setup();
    const api = await import('@/lib/api');
    vi.mocked(api.savePaper).mockRejectedValueOnce(new Error('500 Internal Server Error'));
    vi.mocked(toast.error).mockClear();

    renderFeedView('inbox');

    const saveBtn = await screen.findByRole('button', {
      name: /save surface callback test paper/i,
    });
    await user.click(saveBtn);

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        expect.stringContaining('save paper'),
        expect.objectContaining({ description: '500 Internal Server Error' }),
      );
    });
  });
});

describe('FeedView — shortcut callback freshness', () => {
  // Renders FeedView inside a MemoryRouter with a sentinel /paper/:id route so
  // we can assert on navigate() calls triggered by keyboard shortcuts.
  function NavigationCapture({ onNavigate }: { onNavigate: (path: string) => void }) {
    const loc = useLocation();
    onNavigate(loc.pathname);
    return null;
  }

  function renderWithNav() {
    let capturedPath = '/';
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route
              path="/"
              element={
                <>
                  <FeedView surface="inbox" />
                  <NavigationCapture onNavigate={(p) => { capturedPath = p; }} />
                </>
              }
            />
            <Route
              path="/paper/:id"
              element={<NavigationCapture onNavigate={(p) => { capturedPath = p; }} />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return { getPath: () => capturedPath };
  }

  const freshPaper: FeedPaper = {
    id: 42,
    external_id: 'arxiv:2601.00042',
    source_type: 'arxiv',
    title: 'Fresh Shortcut Paper',
    authors: ['Carol Researcher'],
    abstract: 'Abstract for shortcut freshness test.',
    published_date: '2026-02-01',
    url: 'https://example.com/paper/42',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    discovered_at: null,
    citation_count: 0,
    priority_score: 0.7,
    metadata: {},
    created_at: '2026-02-02T00:00:00Z',
    summary_brief: 'Brief',
    tldr: null,
    confidence: null,
    rating: null,
    has_chunks: false,
    has_summary: false,
    state: 'inbox',
    state_before_trash: null,
    starred: false,
    discovery_origin: 'pulse',
    user_state: {
      state: 'inbox',
      state_before_trash: null,
      starred: false,
      rating: null,
      user_notes: null,
      flagged: false,
      updated_at: null,
    },
    recent_feedback: null,
  };

  it('o key navigates to the paper at focused index (shortcutCallbacks.onOpenDetail is not stale)', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockResolvedValue({ papers: [freshPaper], total: 1 });

    const { getPath } = renderWithNav();

    // Wait for the paper row to appear — focusedIdx defaults to 0 so paper[0] is selected
    await screen.findByRole('button', { name: /save fresh shortcut paper/i });

    // Press o — onOpenDetail(papers[0].id) should navigate to /paper/42
    act(() => {
      fireEvent.keyDown(document.body, { key: 'o' });
    });

    await waitFor(() => expect(getPath()).toBe('/paper/42'));
  });

  it('o key uses the updated paper list when papers change between renders', async () => {
    const api = await import('@/lib/api');

    const paperA: FeedPaper = { ...freshPaper, id: 10, title: 'Paper A' };
    const paperB: FeedPaper = { ...freshPaper, id: 20, title: 'Paper B' };

    // First load returns paper A; second fetch (query refetch) returns paper B
    vi.mocked(api.fetchFeed)
      .mockResolvedValueOnce({ papers: [paperA], total: 1 })
      .mockResolvedValueOnce({ papers: [paperB], total: 1 });

    const { getPath } = renderWithNav();

    // Wait for paper A
    await screen.findByRole('button', { name: /save paper a/i });

    // Press o → should navigate to /paper/10 (paper A's id, index 0)
    act(() => {
      fireEvent.keyDown(document.body, { key: 'o' });
    });

    await waitFor(() => expect(getPath()).toBe('/paper/10'));
  });
});

describe('FeedView — server-side search debounce', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function renderWithFilter(surface: Parameters<typeof FeedView>[0]['surface'], listFilter: string) {
    return render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <FeedView surface={surface} listFilter={listFilter} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('sends q param after 300ms debounce for library surface with ≥3-char filter', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();

    renderWithFilter('library', 'neural');

    // Wait for initial fetch (serverQuery='', no q)
    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());

    vi.mocked(api.fetchFeed).mockClear();

    // Advance past the 300ms debounce window
    await act(async () => { vi.advanceTimersByTime(350); });

    // A fetch with q='neural' should have been triggered
    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ q: 'neural' }),
      ),
    );
  });

  it('sends q param after 300ms debounce for inbox surface with ≥3-char filter', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();

    renderWithFilter('inbox', 'bert');

    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());

    vi.mocked(api.fetchFeed).mockClear();

    await act(async () => { vi.advanceTimersByTime(350); });

    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ q: 'bert' }),
      ),
    );
  });

  it('does not send q for a filter shorter than 3 chars', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();

    renderWithFilter('library', 'ai');

    await act(async () => { vi.advanceTimersByTime(400); });
    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());

    // All calls should have no q or q=undefined
    for (const [params] of vi.mocked(api.fetchFeed).mock.calls) {
      expect((params as { q?: string }).q).toBeUndefined();
    }
  });

  it('does not send q before the 300ms debounce window elapses', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();

    renderWithFilter('library', 'transformer');

    // Wait for initial query (serverQuery='') to fire
    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());

    const callCount = vi.mocked(api.fetchFeed).mock.calls.length;

    // Advance only 100ms — debounce not yet fired, no new calls expected
    await act(async () => { vi.advanceTimersByTime(100); });

    // Same number of calls — debounce hasn't fired
    expect(vi.mocked(api.fetchFeed).mock.calls.length).toBe(callCount);
    // None of the calls so far should have q set
    for (const [params] of vi.mocked(api.fetchFeed).mock.calls) {
      expect((params as { q?: string }).q).toBeUndefined();
    }
  });

  it('does not send q for trash surface even with ≥3-char filter', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();

    renderWithFilter('trash', 'neural');

    await act(async () => { vi.advanceTimersByTime(400); });
    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());

    for (const [params] of vi.mocked(api.fetchFeed).mock.calls) {
      expect((params as { q?: string }).q).toBeUndefined();
    }
  });

  it('does not re-filter server search results client-side', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.fetchFeed).mockClear();
    vi.mocked(api.fetchFeed).mockResolvedValue({ papers: [mockPaper], total: 1 });

    // 'neural' matches this paper server-side (e.g. via abstract FTS) but is
    // NOT a substring of its title or authors — the overlay must not hide it.
    renderWithFilter('library', 'neural');

    await waitFor(() => expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled());
    await act(async () => { vi.advanceTimersByTime(350); });
    await waitFor(() =>
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalledWith(
        expect.objectContaining({ q: 'neural' }),
      ),
    );

    expect(await screen.findByText('Surface Callback Test Paper')).toBeInTheDocument();
  });
});

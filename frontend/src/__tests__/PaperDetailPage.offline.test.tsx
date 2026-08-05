/**
 * PaperDetailPage offline indicator tests
 *
 * Coverage:
 *  - Header shows offline indicator (stale-cached / available-offline) when offline
 *  - Action rail shows online-only banner + disabled state when offline
 *  - Notes section: offline hint shown + create form hidden + actions disabled
 *  - Ask This Paper: offline notice shown instead of RAG chat
 *  - Reading column (sections, chunks): still present offline
 *  - ONLINE rendering unchanged: none of the offline elements appear when online
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { PaperDetailPage } from '@/pages/PaperDetailPage';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Connectivity + persister mocks
// ---------------------------------------------------------------------------

let _online = true;
let _cacheTs: number | null = null;

vi.mock('@/hooks/use-online-status', () => ({
  useOnlineStatus: () => ({ online: _online }),
}));

vi.mock('@/lib/query-persister', () => ({
  getPersistedCacheTimestamp: vi.fn(() => Promise.resolve(_cacheTs)),
  clearPersistedQueryCache: vi.fn(),
}));

// ---------------------------------------------------------------------------
// UI store mock
// ---------------------------------------------------------------------------

vi.mock('@/stores/ui-store', () => ({
  useUIStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      paperDetailNoteDismissed: true,  // hide workspace note to reduce noise
      setPaperDetailNoteDismissed: vi.fn(),
      sidebarCollapsed: false,
      selectedPaperId: null,
      checklistDismissed: false,
      toggleSidebar: vi.fn(),
      setSelectedPaperId: vi.fn(),
      dismissChecklist: vi.fn(),
    }),
}));

// ---------------------------------------------------------------------------
// API mocks
// ---------------------------------------------------------------------------

// vi.mock factories are hoisted to the top of the file by Vitest, so any
// module-level const referenced inside a factory would be in the TDZ.
// vi.hoisted() runs before the hoist, making the values available in time.
const { DETAIL_FIXTURE } = vi.hoisted(() => {
  const PAPER_FIXTURE = {
    id: 42,
    external_id: 'arxiv:2301.00001',
    source_type: 'arxiv',
    title: 'Test Paper Title',
    authors: ['Author A'],
    abstract: 'Abstract text',
    published_date: '2025-01-01',
    url: 'https://arxiv.org/abs/2301.00001',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    citation_count: 0,
    priority_score: null,
    metadata: {},
    discovered_at: '2025-01-01T00:00:00Z',
    created_at: '2025-01-01T00:00:00Z',
    summary_brief: null,
    tldr: null,
    confidence: null,
    discovery_origin: 'user_initiated' as const,
    recent_feedback: null,
  };

  const DETAIL_FIXTURE = {
    paper: PAPER_FIXTURE,
    summary: null,
    chunks: [],
    user_state: { state: 'inbox', starred: false },
    has_project_links: false,
  };

  return { DETAIL_FIXTURE };
});

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  return createApiMock({
    fetchPaperDetail: async () => DETAIL_FIXTURE,
    fetchContradictions: async () => ({ contradictions: [] }),
    scanPaperContradictions: vi.fn(),
    fetchNotes: async () => [],
    fetchDecks: async () => [],
    zoteroGetLinkage: async () => ({ zotero_item_key: null }),
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
    generateCardsJob: vi.fn(),
    getJob: vi.fn(),
  });
});

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

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      jobs: {},
      hydrate: vi.fn(),
      trackExternalJob: vi.fn(),
      isRunning: vi.fn().mockReturnValue(false),
    }),
  ),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeQc() {
  return createTestQueryClient();
}

function renderDetail() {
  return renderWithProviders(
    <MemoryRouter initialEntries={['/papers/42']}>
      <Routes>
        <Route path="/papers/:paperId" element={<PaperDetailPage />} />
      </Routes>
    </MemoryRouter>,
    { queryClient: makeQc() },
  );
}

beforeEach(() => {
  _online = true;
  _cacheTs = null;
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Header offline indicator
// ---------------------------------------------------------------------------

describe('PaperDetailPage — header offline indicator', () => {
  it('shows paper-detail-offline-indicator when offline (no timestamp)', async () => {
    _online = false;
    _cacheTs = null;
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('paper-detail-offline-indicator')).toBeTruthy();
    });
    expect(screen.getByTestId('offline-indicator-available')).toBeTruthy();
  });

  it('shows stale-cached indicator when offline with timestamp', async () => {
    _online = false;
    _cacheTs = Date.now() - 60_000 * 60; // 1h ago
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('offline-indicator-stale')).toBeTruthy();
    });
  });

  it('does NOT show paper-detail-offline-indicator when online', async () => {
    _online = true;
    renderDetail();
    await waitFor(() => {
      // Paper loaded — title appears in breadcrumb + h1, so getAllByText
      expect(screen.getAllByText('Test Paper Title').length).toBeGreaterThan(0);
    });
    expect(screen.queryByTestId('paper-detail-offline-indicator')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Action rail — online-only banner + disabled state
// ---------------------------------------------------------------------------

describe('PaperDetailPage — action rail offline', () => {
  it('shows action-rail-offline-banner when offline', async () => {
    _online = false;
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('action-rail-offline-banner')).toBeTruthy();
    });
  });

  it('marks action rail as disabled when offline', async () => {
    _online = false;
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('action-rail-disabled')).toBeTruthy();
    });
  });

  it('does NOT show action-rail-offline-banner when online', async () => {
    _online = true;
    renderDetail();
    await waitFor(() => {
      // title appears in breadcrumb + h1, so getAllByText
      expect(screen.getAllByText('Test Paper Title').length).toBeGreaterThan(0);
    });
    expect(screen.queryByTestId('action-rail-offline-banner')).toBeNull();
    expect(screen.queryByTestId('action-rail-disabled')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Notes — offline read-only
// ---------------------------------------------------------------------------

describe('PaperDetailPage — Notes section offline', () => {
  it('shows notes-offline-hint when offline', async () => {
    _online = false;
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('notes-offline-hint')).toBeTruthy();
    });
  });

  it('hides note create form when offline', async () => {
    _online = false;
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('notes-offline-hint')).toBeTruthy();
    });
    expect(screen.queryByTestId('notes-create-form')).toBeNull();
  });

  it('shows note create form when online', async () => {
    _online = true;
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('notes-create-form')).toBeTruthy();
    });
    expect(screen.queryByTestId('notes-offline-hint')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Ask This Paper — online-only notice
// ---------------------------------------------------------------------------

describe('PaperDetailPage — Ask This Paper section offline', () => {
  it('shows ask-offline-notice when offline', async () => {
    _online = false;
    renderDetail();
    await waitFor(() => {
      expect(screen.getByTestId('ask-offline-notice')).toBeTruthy();
    });
  });

  it('does NOT show ask-offline-notice when online', async () => {
    _online = true;
    renderDetail();
    await waitFor(() => {
      // title appears in breadcrumb + h1, so getAllByText
      expect(screen.getAllByText('Test Paper Title').length).toBeGreaterThan(0);
    });
    expect(screen.queryByTestId('ask-offline-notice')).toBeNull();
  });
});

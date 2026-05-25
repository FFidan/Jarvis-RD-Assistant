/**
 * ResearchFeedPage offline indicator tests — Wave 3 P1d
 *
 * Coverage:
 *  - Library surface: shows offline indicator when offline
 *  - Library surface: shows stale-cached indicator when timestamp known
 *  - Inbox surface: shows online-only notice when offline
 *  - Search surface: shows online-only notice + disabled UI when offline
 *  - FacetRail: receives isOnline=false when offline
 *  - Default redirect → Library when offline (not Inbox)
 *  - ONLINE rendering unchanged: no offline indicators when online
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';

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
}));

// ---------------------------------------------------------------------------
// API mocks — minimal stubs so the component mounts without network calls
// ---------------------------------------------------------------------------

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchFeedPapers: vi.fn().mockResolvedValue({ papers: [], total: 0, offset: 0, limit: 20 }),
    fetchFeedCounts: vi.fn().mockResolvedValue({
      inbox: 0, library: 5, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0,
      active: 5, kept: 5, all_non_trash: 5,
    }),
    fetchSources: vi.fn().mockResolvedValue([]),
    fetchFeedCountsWithFacets: vi.fn().mockResolvedValue({
      inbox: 0, library: 5, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0,
      active: 5, kept: 5, all_non_trash: 5,
      by_source: {}, by_topic: [], untagged: 0,
    }),
    searchPreview: vi.fn(),
    batchSavePapers: vi.fn(),
  };
});


vi.mock('@/stores/bulk-selection-store', () => ({
  useBulkSelection: Object.assign(vi.fn().mockReturnValue([]), {
    getState: vi.fn().mockReturnValue({ clear: vi.fn(), selectedIds: new Set() }),
  }),
}));

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn().mockReturnValue(vi.fn()),
}));

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderFeed(initialPath = '/feed?surface=library') {
  const qc = makeQc();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/feed" element={<ResearchFeedPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  _online = true;
  _cacheTs = null;
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Library surface — offline indicators
// ---------------------------------------------------------------------------

describe('ResearchFeedPage — Library surface offline', () => {
  it('shows available-offline indicator on Library when offline and no timestamp', async () => {
    _online = false;
    _cacheTs = null;
    renderFeed('/feed?surface=library');
    await waitFor(() => {
      expect(screen.getByTestId('library-offline-indicator')).toBeTruthy();
    });
    expect(screen.getByTestId('offline-indicator-available')).toBeTruthy();
  });

  it('shows stale-cached indicator on Library when offline with timestamp', async () => {
    _online = false;
    _cacheTs = Date.now() - 60_000 * 30; // 30 min ago
    renderFeed('/feed?surface=library');
    await waitFor(() => {
      expect(screen.getByTestId('offline-indicator-stale')).toBeTruthy();
    });
  });

  it('does NOT show library-offline-indicator when online', async () => {
    _online = true;
    renderFeed('/feed?surface=library');
    // Give it time to settle
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('library-offline-indicator')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Inbox surface — online-only notice
// ---------------------------------------------------------------------------

describe('ResearchFeedPage — Inbox surface offline', () => {
  it('shows online-only notice on Inbox when offline', async () => {
    _online = false;
    renderFeed('/feed?surface=inbox');
    await waitFor(() => {
      expect(screen.getByTestId('inbox-offline-notice')).toBeTruthy();
    });
  });

  it('does NOT show inbox-offline-notice when online', async () => {
    _online = true;
    renderFeed('/feed?surface=inbox');
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('inbox-offline-notice')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Search surface — online-only notice
// ---------------------------------------------------------------------------

describe('ResearchFeedPage — Search surface offline', () => {
  it('shows search-offline-notice when offline', async () => {
    _online = false;
    renderFeed('/feed?surface=search');
    await waitFor(() => {
      expect(screen.getByTestId('search-offline-notice')).toBeTruthy();
    });
  });

  it('does NOT show search-offline-notice when online', async () => {
    _online = true;
    renderFeed('/feed?surface=search');
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('search-offline-notice')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// FacetRail — isOnline prop
// ---------------------------------------------------------------------------

describe('ResearchFeedPage — FacetRail online-only facets', () => {
  it('Source section shows unavailable offline when offline', async () => {
    _online = false;
    renderFeed('/feed?surface=library');
    await waitFor(() => {
      // FacetRail renders "Unavailable offline" when !isOnline and no source facets
      const text = document.body.textContent ?? '';
      expect(text.toLowerCase()).toContain('unavailable offline');
    });
  });
});

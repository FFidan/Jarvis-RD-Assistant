import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { useBulkSelection } from '@/stores/bulk-selection-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

vi.mock('@/components/chat/StreamingChat', () => ({
  StreamingChat: () => <div data-testid="streaming-chat" />,
}));

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  const paper = {
    id: 7,
    external_id: 'arxiv:2603.00007',
    source_type: 'arxiv' as const,
    title: 'Library Paper For Bulk',
    authors: ['Lib Author'],
    abstract: 'Library paper.',
    published_date: '2026-03-01',
    url: 'https://example.com/paper/7',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    citation_count: 0,
    priority_score: 0.5,
    metadata: {},
    discovered_at: '2026-03-02T00:00:00Z',
    created_at: '2026-03-02T00:00:00Z',
    summary_brief: null,
    tldr: null,
    confidence: null,
    rating: null,
    has_chunks: false,
    has_summary: false,
    user_state: {
      saved: true,
      starred: false,
      status: 'new' as const,
      dismissed: false,
      archived: false,
      preference: 'none' as const,
      rating: null,
      user_notes: null,
      flagged: false,
      updated_at: null,
    },
  };
  return createApiMock({
    fetchFeed: async () => ({ papers: [paper], total: 1 }),
    bulkAction: async () => ({ succeeded: [7], failed: [] }),
    fetchSources: async () => [],
    fetchTopics: async () => [],
    fetchFeedCounts: async () => ({
      inbox: 1, library: 1, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 2,
    }),
    fetchFeedCountsWithFacets: async () => ({
      inbox: 1, library: 1, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0,
      active: 1, kept: 1, all_non_trash: 1,
      by_source: {}, by_topic: [], untagged: 0,
    }),
  });
});

function makeClient() {
  return createTestQueryClient();
}

function renderAt(initialEntry: string) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/feed" element={<ResearchFeedPage />} />
      </Routes>
    </MemoryRouter>,
    { queryClient: makeClient() },
  );
}

describe('ResearchFeedPage — bulk selection clears on surface change', () => {
  beforeEach(() => {
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  it('clicking a different surface chip clears bulk selection', async () => {
    const user = userEvent.setup();
    renderAt('/feed?surface=library');

    // Wait for the library surface to render the paper row
    await waitFor(() =>
      expect(
        screen.getByLabelText(/select library paper for bulk for bulk action/i),
      ).toBeInTheDocument(),
    );

    // Toggle bulk selection on the row
    await user.click(
      screen.getByLabelText(/select library paper for bulk for bulk action/i),
    );
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(1);
    });

    // Switch to the Trash surface via §Status facet (Trash is a facet, not a tab)
    await user.click(screen.getByTestId('facet-status-trash'));

    // Selection should now be empty
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(0);
    });
  });
});

describe('ResearchFeedPage — bulk selection clears on URL-driven surface change', () => {
  beforeEach(() => {
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  it('clears bulk selection on URL-driven surface change (back button)', async () => {
    // Seed a bulk selection before rendering
    useBulkSelection.setState({ selectedIds: new Set([42]) });
    expect(useBulkSelection.getState().selectedIds.size).toBe(1);

    const { rerender } = render(
      <QueryClientProvider client={makeClient()}>
        <MemoryRouter initialEntries={['/feed?surface=inbox']}>
          <Routes>
            <Route path="/feed" element={<ResearchFeedPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // Simulate a URL-driven surface change (e.g. browser back/forward) by
    // re-rendering the tree with a new MemoryRouter pointing at trash.
    rerender(
      <QueryClientProvider client={makeClient()}>
        <MemoryRouter initialEntries={['/feed?surface=trash']}>
          <Routes>
            <Route path="/feed" element={<ResearchFeedPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // The useEffect([surface]) should have fired and cleared the store.
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(0);
    });
  });
});

// Helper: a sibling component inside the same Router that can call setSearchParams
// directly — NOT via a ResearchFeedPage surface chip.
function SurfaceSwitcher() {
  const [, setSearchParams] = useSearchParams();
  return (
    <button
      data-testid="direct-surface-switcher"
      onClick={() => setSearchParams({ surface: 'trash' })}
    >
      Switch to trash
    </button>
  );
}

describe('ResearchFeedPage — bulk clears on direct setSearchParams', () => {
  beforeEach(() => {
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  it('clears bulk selection when URL surface param changes via direct setSearchParams', async () => {
    const user = userEvent.setup();

    // Pre-seed bulk selection directly via store — bypasses any UI interaction
    useBulkSelection.setState({ selectedIds: new Set([99]) });
    expect(useBulkSelection.getState().selectedIds.size).toBe(1);

    renderWithProviders(
      <MemoryRouter initialEntries={['/feed?surface=inbox']}>
        <Routes>
          <Route
            path="/feed"
            element={
              <>
                <SurfaceSwitcher />
                <ResearchFeedPage />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
      { queryClient: makeClient() },
    );

    // Confirm the page has rendered (surface=inbox is the default content)
    await waitFor(() => {
      expect(screen.getByTestId('direct-surface-switcher')).toBeInTheDocument();
    });

    // Trigger the URL surface change directly via setSearchParams — NOT by
    // clicking any chip inside ResearchFeedPage.
    await user.click(screen.getByTestId('direct-surface-switcher'));

    // The useEffect([surface]) in ResearchFeedPage must fire and clear the store.
    // If that useEffect is removed, selectedIds.size stays 1 and this assertion fails.
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(0);
    });
  });
});

// A sibling inside the same Router that flips one query param to a target value
// WITHOUT remounting ResearchFeedPage (so only a deps change can re-fire the effect).
function ParamSwitcher({ param, value }: { param: string; value: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  return (
    <button
      data-testid="param-switcher"
      onClick={() => {
        const next = new URLSearchParams(searchParams);
        next.set(param, value);
        setSearchParams(next);
      }}
    >
      switch
    </button>
  );
}

describe('ResearchFeedPage — bulk clears on filter/facet changes (FEE-1)', () => {
  beforeEach(() => {
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  async function seedThenSwitch(initialEntry: string, param: string, value: string) {
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/feed"
            element={
              <>
                <ParamSwitcher param={param} value={value} />
                <ResearchFeedPage />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
      { queryClient: makeClient() },
    );

    // Page mounted → the mount-effect already cleared. Re-seed AFTER mount so the
    // selection is live going into the param change (otherwise we'd be asserting
    // the spurious mount-clear, not the deps-driven clear).
    await waitFor(() =>
      expect(screen.getByTestId('param-switcher')).toBeInTheDocument(),
    );
    act(() => {
      useBulkSelection.setState({ selectedIds: new Set([99]) });
    });
    expect(useBulkSelection.getState().selectedIds.size).toBe(1);

    await user.click(screen.getByTestId('param-switcher'));
    return user;
  }

  it('clears bulk selection when ?filter changes (Reading→Done)', async () => {
    await seedThenSwitch('/feed?surface=library&filter=reading', 'filter', 'done');
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(0);
    });
  });

  it('clears bulk selection when ?facet_source changes', async () => {
    await seedThenSwitch('/feed?surface=library', 'facet_source', 'arxiv');
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(0);
    });
  });

  it('clears bulk selection when ?facet_topic changes', async () => {
    await seedThenSwitch('/feed?surface=library', 'facet_topic', '3');
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(0);
    });
  });
});

describe('ResearchFeedPage — invalid ?surface= falls back to inbox', () => {
  beforeEach(() => {
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  it('surface=__proto__ renders inbox UI', async () => {
    renderAt('/feed?surface=__proto__');

    // Inbox §Status facet should be active (aria-pressed=true) in the facet rail.
    await waitFor(() => {
      expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
    // And the inbox SectionInfo description should be present
    expect(
      screen.getByText(/Unread papers from your configured sources/i),
    ).toBeInTheDocument();
  });
});

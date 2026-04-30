import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { useBulkSelection } from '@/stores/bulk-selection-store';

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('@/components/chat/StreamingChat', () => ({
  StreamingChat: () => <div data-testid="streaming-chat" />,
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
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
    user_status: 'new',
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
  return {
    ...actual,
    fetchFeed: vi.fn().mockResolvedValue({ papers: [paper], total: 1 }),
    bulkAction: vi.fn().mockResolvedValue({ succeeded: [7], failed: [] }),
    fetchSources: vi.fn().mockResolvedValue([]),
    fetchTopics: vi.fn().mockResolvedValue([]),
    useFeedCounts: vi.fn().mockReturnValue({
      data: { inbox: 1, library: 1, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 2 },
      isLoading: false,
      isPending: false,
    }),
    fetchFeedCounts: vi.fn().mockResolvedValue({
      inbox: 1, library: 1, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 2,
    }),
  };
});

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderAt(initialEntry: string) {
  return render(
    <QueryClientProvider client={makeClient()}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/feed" element={<ResearchFeedPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ResearchFeedPage — bulk selection clears on surface change (NEW-H5)', () => {
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

    // Switch to the Trash surface chip
    await user.click(screen.getByRole('tab', { name: 'Trash' }));

    // Selection should now be empty
    await waitFor(() => {
      expect(useBulkSelection.getState().selectedIds.size).toBe(0);
    });
  });
});

describe('ResearchFeedPage — bulk selection clears on URL-driven surface change (NEW-H5-URL)', () => {
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

describe('ResearchFeedPage — invalid ?surface= falls back to inbox (NEW-M16)', () => {
  beforeEach(() => {
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  it('surface=__proto__ renders inbox UI', async () => {
    renderAt('/feed?surface=__proto__');

    // Inbox tab should be the active tab (aria-selected=true). The accessible
    // name may include a count-badge suffix (e.g. "Inbox 1"), so match by regex.
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /^Inbox/ })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });
    // And the inbox SectionInfo description should be present
    expect(
      screen.getByText(/Unread papers from your configured sources/i),
    ).toBeInTheDocument();
  });
});

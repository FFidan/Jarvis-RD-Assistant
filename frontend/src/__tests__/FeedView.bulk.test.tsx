import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { FeedView } from '@/components/feed/FeedView';
import { useBulkSelection } from '@/stores/bulk-selection-store';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  const paper = {
    id: 42,
    external_id: 'arxiv:2602.00042',
    source_type: 'arxiv' as const,
    title: 'Bulk Selection Test Paper',
    authors: ['Bulk Author'],
    abstract: 'Tests bulk wiring.',
    published_date: '2026-02-01',
    url: 'https://example.com/paper/42',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    citation_count: 0,
    priority_score: 0.5,
    metadata: {},
    discovered_at: '2026-02-02T00:00:00Z',
    created_at: '2026-02-02T00:00:00Z',
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
    bulkAction: vi.fn().mockResolvedValue({ succeeded: [42], failed: [] }),
    savePaper: vi.fn().mockResolvedValue({ ok: true }),
    unsavePaper: vi.fn().mockResolvedValue({ ok: true }),
    dismissPaper: vi.fn().mockResolvedValue({ ok: true }),
    restorePaper: vi.fn().mockResolvedValue({ ok: true }),
    hardDeletePaper: vi.fn().mockResolvedValue({ ok: true }),
    markPaperRead: vi.fn().mockResolvedValue({ ok: true }),
    bookmarkPaper: vi.fn().mockResolvedValue({ ok: true }),
    archivePaper: vi.fn().mockResolvedValue({ ok: true }),
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

function renderFeedView() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter>
        <FeedView surface="library" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('FeedView — bulk selection wiring (NEW-H4)', () => {
  beforeEach(() => {
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  it('renders BulkToolbar after a row checkbox is toggled', async () => {
    const user = userEvent.setup();
    renderFeedView();

    // Wait for the row to render
    await waitFor(() =>
      expect(
        screen.getByLabelText(/select bulk selection test paper for bulk action/i),
      ).toBeInTheDocument(),
    );

    // Initially the bulk toolbar is hidden (selection is empty)
    expect(screen.queryByText(/\d+ selected/)).not.toBeInTheDocument();

    // Click the row's bulk checkbox
    await user.click(
      screen.getByLabelText(/select bulk selection test paper for bulk action/i),
    );

    // BulkToolbar should appear with the selection count
    await waitFor(() => {
      expect(screen.getByText('1 selected')).toBeInTheDocument();
    });
    // Library surface offers Star + Archive + Mark Read + Dismiss + Unsave actions
    expect(screen.getByRole('button', { name: 'Star' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Archive' })).toBeInTheDocument();
  });
});

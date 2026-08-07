import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { FeedView } from '@/components/feed/FeedView';
import { useBulkSelection } from '@/stores/bulk-selection-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  const { makeFeedPaper } = await import('@/__tests__/fixtures/feed-paper');
  const paper = makeFeedPaper({
    id: 42,
    external_id: 'arxiv:2602.00042',
    title: 'Bulk Selection Test Paper',
    authors: ['Bulk Author'],
    abstract: 'Tests bulk wiring.',
    published_date: '2026-02-01',
    url: 'https://example.com/paper/42',
    priority_score: 0.5,
    created_at: '2026-02-02T00:00:00Z',
    discovered_at: null,
    summary_brief: null,
    tldr: null,
    confidence: null,
    has_chunks: false,
    has_summary: false,
    state: 'to_read',
    user_state: {
      state: 'to_read',
      state_before_trash: null,
      starred: false,
      rating: null,
      user_notes: null,
      flagged: false,
      updated_at: null,
    },
  });
  return createApiMock({
    fetchFeed: async () => ({ papers: [paper], total: 1 }),
    bulkAction: async () => ({ succeeded: [42], failed: [] }),
    savePaper: async () => ({ ok: true }),
    skipPaper: async () => ({ status: 'ok', paper_id: 42 }),
    markReading: async () => ({ status: 'ok', paper_id: 42 }),
    markDone: async () => ({ status: 'ok', paper_id: 42 }),
    trashPaper: async () => ({ status: 'ok', paper_id: 42 }),
    restorePaper: async () => ({ ok: true }),
    hardDeletePaper: async () => ({ deleted: 1 }),
    starPaper: async () => ({ status: 'ok', paper_id: 42 }),
    unstarPaper: async () => ({ status: 'ok', paper_id: 42 }),
  });
});

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

function makeQueryClient() {
  return createTestQueryClient();
}

function renderFeedView() {
  return renderWithProviders(
    <MemoryRouter>
      <FeedView surface="library" />
    </MemoryRouter>,
    { queryClient: makeQueryClient() },
  );
}

describe('FeedView — bulk selection wiring', () => {
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
    // Library surface offers Mark Reading + Mark Done + Trash + Star/Unstar
    expect(screen.getByRole('button', { name: 'Mark Reading' })).toBeInTheDocument();
  });
});

import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { toast } from 'sonner';
import { FeedView } from '@/components/feed/FeedView';
import type { FeedPaper } from '@/types';

// Minimal FeedPaper fixture used across all surface tests
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
  citation_count: 0,
  priority_score: 0.5,
  metadata: {},
  discovered_at: '2026-01-02T00:00:00Z',
  created_at: '2026-01-02T00:00:00Z',
  summary_brief: 'Brief summary',
  tldr: null,
  confidence: null,
  user_status: 'new',
  rating: null,
  has_chunks: false,
  has_summary: false,
  user_state: {
    saved: false,
    starred: false,
    status: 'new',
    dismissed: false,
    archived: false,
    preference: 'none',
    rating: null,
    user_notes: null,
    flagged: false,
    updated_at: null,
  },
};

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
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
    pdf_local_path: null,
    pdf_downloaded: false,
    citation_count: 0,
    priority_score: 0.5,
    metadata: {},
    discovered_at: '2026-01-02T00:00:00Z',
    created_at: '2026-01-02T00:00:00Z',
    summary_brief: 'Brief summary',
    tldr: null,
    confidence: null,
    user_status: 'new',
    rating: null,
    has_chunks: false,
    has_summary: false,
    user_state: {
      saved: false,
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

function renderFeedView(surface: Parameters<typeof FeedView>[0]['surface']) {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter>
        <FeedView surface={surface} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('FeedView — surface-aware callback spreading (NEW-C2)', () => {
  it('surface="inbox" renders a Save button for each paper', async () => {
    renderFeedView('inbox');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /save surface callback test paper/i })).toBeInTheDocument(),
    );
  });

  it('surface="library" renders a Star button for each paper', async () => {
    renderFeedView('library');
    // The star button is icon-only with aria-label "Star <title>"
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /star surface callback test paper/i })).toBeInTheDocument(),
    );
  });

  it('surface="archived" renders an Unarchive button for each paper', async () => {
    renderFeedView('archived');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /unarchive surface callback test paper/i })).toBeInTheDocument(),
    );
  });

  it('surface="trash" renders Restore and Permanently-delete buttons for each paper', async () => {
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

describe('FeedView — mutation onError toasts (NEW-H3)', () => {
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
        expect.stringContaining('500 Internal Server Error'),
      );
    });
  });
});

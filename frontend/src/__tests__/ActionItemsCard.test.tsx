import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { ActionItemsCard } from '@/components/my-day/ActionItemsCard';
import * as api from '@/lib/api';
import type { FeedResponse } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchFeedPapers: vi.fn(),
  };
});

const mockStartJob = vi.fn().mockResolvedValue('job-1');

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      isRunning: () => false,
      startJob: mockStartJob,
    }),
  ),
}));

const emptyFeed: FeedResponse = { papers: [], total: 0 };

const makePaper = (id: number, title: string, pdf_downloaded = true) => ({
  id, external_id: `arxiv:00${id}`, source_type: 'arxiv' as const, title,
  authors: [], abstract: null, published_date: null, url: '', pdf_url: null,
  pdf_local_path: null, pdf_downloaded, citation_count: 0,
  priority_score: null, metadata: {},
  discovered_at: null, created_at: '', summary_brief: null, tldr: null,
  confidence: null, user_status: 'new' as const, rating: null,
});

const paperFeed: FeedResponse = {
  papers: [makePaper(1, 'Paper Needs Processing')],
  total: 1,
};

const threePaperFeed: FeedResponse = {
  papers: [
    makePaper(1, 'Paper One'),
    makePaper(2, 'Paper Two'),
    makePaper(3, 'Paper Three'),
  ],
  total: 3,
};

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ActionItemsCard />
      </BrowserRouter>
    </QueryClientProvider>,
  );
}

describe('ActionItemsCard', () => {
  beforeEach(() => {
    mockStartJob.mockReset();
    mockStartJob.mockResolvedValue('job-1');
  });

  it('shows "all caught up" when no action items', async () => {
    vi.mocked(api.fetchFeedPapers).mockResolvedValue(emptyFeed);
    renderWithProviders();
    expect(await screen.findByText("You're all caught up")).toBeInTheDocument();
  });

  it('shows unprocessed paper title', async () => {
    vi.mocked(api.fetchFeedPapers).mockResolvedValue(paperFeed);
    renderWithProviders();
    expect(await screen.findByText('Paper Needs Processing')).toBeInTheDocument();
  });

  it('shows "Process all" button when processable papers exist', async () => {
    vi.mocked(api.fetchFeedPapers).mockResolvedValue(paperFeed);
    renderWithProviders();
    expect(await screen.findByText(/Process all/)).toBeInTheDocument();
  });

  it('renders skeleton while loading', () => {
    vi.mocked(api.fetchFeedPapers).mockReturnValue(new Promise(() => {}));
    renderWithProviders();
    const skeletons = document.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows process link for each paper', async () => {
    vi.mocked(api.fetchFeedPapers).mockResolvedValue(paperFeed);
    renderWithProviders();
    const processBtn = await screen.findByText('Process');
    expect(processBtn).toBeInTheDocument();
    // Link should point to paper detail with ?action=process
    const link = processBtn.closest('a');
    expect(link?.getAttribute('href')).toMatch(/\/paper\/1\?action=process/);
  });

  it('shows error banner and not "all caught up" when query fails', async () => {
    vi.mocked(api.fetchFeedPapers).mockRejectedValue(new Error('Network error'));
    renderWithProviders();
    expect(await screen.findByText(/Could not load action items/i)).toBeInTheDocument();
    expect(screen.queryByText("You're all caught up")).not.toBeInTheDocument();
  });

  it('shows retry button in error state that re-fires the query', async () => {
    vi.mocked(api.fetchFeedPapers)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValue(emptyFeed);
    renderWithProviders();
    const retryBtn = await screen.findByRole('button', { name: /retry/i });
    expect(retryBtn).toBeInTheDocument();
    fireEvent.click(retryBtn);
    expect(await screen.findByText("You're all caught up")).toBeInTheDocument();
  });

  it('Process All fires all jobs in parallel (not sequentially)', async () => {
    // startJob never resolves — so if it were sequential the second call would
    // never happen until the first settled; with Promise.all all 3 are called
    // synchronously before any settle.
    mockStartJob.mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchFeedPapers).mockResolvedValue(threePaperFeed);

    renderWithProviders();

    // Wait for the button to appear (data loaded)
    const btn = await screen.findByText(/Process all/);

    fireEvent.click(btn);

    // All three calls must have been dispatched synchronously inside Promise.all
    await waitFor(() => {
      expect(mockStartJob).toHaveBeenCalledTimes(3);
    });

    expect(mockStartJob).toHaveBeenCalledWith('paper.process', { paper_id: 1 });
    expect(mockStartJob).toHaveBeenCalledWith('paper.process', { paper_id: 2 });
    expect(mockStartJob).toHaveBeenCalledWith('paper.process', { paper_id: 3 });
  });
});

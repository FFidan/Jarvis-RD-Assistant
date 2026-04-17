import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
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

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      startJob: vi.fn().mockResolvedValue('job-1'),
    }),
  ),
}));

const emptyFeed: FeedResponse = { papers: [], total: 0 };

const paperFeed: FeedResponse = {
  papers: [
    {
      id: 1, external_id: 'arxiv:001', source_type: 'arxiv', title: 'Paper Needs Processing',
      authors: [], abstract: null, published_date: null, url: '', pdf_url: null,
      pdf_local_path: null, pdf_downloaded: true, citation_count: 0,
      priority_score: null, metadata: {}, is_read: false,
      discovered_at: null, created_at: '', summary_brief: null, tldr: null,
      confidence: null, user_status: 'new', rating: null,
    },
  ],
  total: 1,
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
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HomePage } from '@/pages/HomePage';

// Mock the api module
vi.mock('@/lib/api', () => ({
  fetchDashboardMetrics: vi.fn(),
  checkHealth: vi.fn(),
  batchProcessPapers: vi.fn(),
  batchSummarizePapers: vi.fn(),
  batchExtractEntities: vi.fn(),
  getSetupStatus: vi.fn().mockResolvedValue({
    setup_completed: true,
    models_ready: true,
    models_downloading: [],
    topics_count: 1,
    telegram_configured: false,
    telegram_paired: false,
  }),
}));

const { fetchDashboardMetrics, batchProcessPapers, batchSummarizePapers, batchExtractEntities } =
  await import('@/lib/api');

function renderHomePage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mockMetrics = {
  total_papers: 42,
  unread_papers: 7,
  pending_papers: 3,
  due_cards: 5,
  active_projects: 2,
  topic_count: 4,
  nudge_count: 6,
  onboarding_stage: 'complete',
};

describe('HomePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the dashboard heading', () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
  });

  it('shows skeleton loaders while loading', () => {
    vi.mocked(fetchDashboardMetrics).mockReturnValue(new Promise(() => {}));
    const { container } = renderHomePage();
    // Skeleton elements have the animate-pulse class
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders metric tiles when data loads', async () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    // Wait for data to render
    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(screen.getByText('Library')).toBeInTheDocument();
    expect(screen.getByText('7 unread · 3 unsummarized')).toBeInTheDocument();
  });

  it('does not render Quick Navigation section', () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.queryByText('Quick Navigation')).not.toBeInTheDocument();
  });

  it('does not render § GET STARTED section marker', () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.queryByText('§ GET STARTED')).toBeNull();
  });

  it('does not render § BATCH OPS section marker', () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.queryByText('§ BATCH OPS')).toBeNull();
  });

  it('renders all five metric tiles when data loads', async () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(await screen.findByText('Library')).toBeInTheDocument();
    expect(screen.getByText('Due Cards')).toBeInTheDocument();
    expect(screen.getByText('Active Projects')).toBeInTheDocument();
    expect(screen.getByText('Topics')).toBeInTheDocument();
    expect(screen.getByText('Scheduled Jobs')).toBeInTheDocument();
  });

  it('renders zero values when metrics are all zeros', async () => {
    const zeroMetrics = {
      total_papers: 0,
      unread_papers: 0,
      pending_papers: 0,
      due_cards: 0,
      active_projects: 0,
      topic_count: 0,
      nudge_count: 0,
    };
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(zeroMetrics);
    renderHomePage();
    expect(await screen.findByText('Library')).toBeInTheDocument();
    // All five tiles should show 0
    const zeros = screen.getAllByText('0');
    expect(zeros.length).toBe(5);
    // Library subtitle shows "All caught up" when unread is 0
    expect(screen.getByText('All caught up')).toBeInTheDocument();
  });

  describe('BatchButton confirmation dialogs', () => {
    beforeEach(() => {
      vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    });

    it('does not call batchProcessPapers when user cancels confirmation', async () => {
      renderHomePage();
      const button = screen.getByRole('button', { name: /Process PDFs/i });
      await userEvent.click(button);
      expect(screen.getByText('Are you sure?')).toBeInTheDocument();
      expect(
        screen.getByText(
          'This will process PDFs for all papers in your library. This may take several minutes. Continue?',
        ),
      ).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
      expect(batchProcessPapers).not.toHaveBeenCalled();
    });

    it('does not call batchSummarizePapers when user cancels confirmation', async () => {
      renderHomePage();
      const button = screen.getByRole('button', { name: /Summarize/i });
      await userEvent.click(button);
      expect(screen.getByText('Are you sure?')).toBeInTheDocument();
      expect(
        screen.getByText(
          'This will generate AI summaries for all unprocessed papers. This costs LLM tokens. Continue?',
        ),
      ).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
      expect(batchSummarizePapers).not.toHaveBeenCalled();
    });

    it('does not call batchExtractEntities when user cancels confirmation', async () => {
      renderHomePage();
      const button = screen.getByRole('button', { name: /Extract Entities/i });
      await userEvent.click(button);
      expect(screen.getByText('Are you sure?')).toBeInTheDocument();
      expect(
        screen.getByText(
          'This will extract entities from all papers. This costs LLM tokens. Continue?',
        ),
      ).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
      expect(batchExtractEntities).not.toHaveBeenCalled();
    });

    it('calls batchProcessPapers when user confirms', async () => {
      vi.mocked(batchProcessPapers).mockResolvedValue({ queued: 5, total_unprocessed: 5, skipped_missing_pdf: 0, job_id: 'job-123' });
      renderHomePage();
      const button = screen.getByRole('button', { name: /Process PDFs/i });
      await userEvent.click(button);
      expect(screen.getByText('Are you sure?')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      await waitFor(() => expect(batchProcessPapers).toHaveBeenCalledTimes(1));
    });

    it('calls batchSummarizePapers and shows queued count', async () => {
      vi.mocked(batchSummarizePapers).mockResolvedValue({ total_unsummarized: 7, job_id: 'job-sum-1' });
      renderHomePage();
      const button = screen.getByRole('button', { name: /Summarize/i });
      await userEvent.click(button);
      expect(screen.getByText('Are you sure?')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      await waitFor(() => expect(batchSummarizePapers).toHaveBeenCalledTimes(1));
      expect(await screen.findByText('Queued 7 papers')).toBeInTheDocument();
    });
  });
});

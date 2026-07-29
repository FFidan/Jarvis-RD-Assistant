import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { HomePage } from '@/pages/HomePage';
import { useUIStore } from '@/stores/ui-store';
import { useAuthStore } from '@/stores/auth-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const { trackExternalJobMock } = vi.hoisted(() => ({
  trackExternalJobMock: vi.fn(),
}));

vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (state: { trackExternalJob: typeof trackExternalJobMock }) => unknown) =>
    selector({ trackExternalJob: trackExternalJobMock }),
}));

// Mock the api module
vi.mock('@/lib/api', () => ({
  fetchDashboardMetrics: vi.fn(),
  checkHealth: vi.fn(),
  batchProcessPapers: vi.fn(),
  batchSummarizePapers: vi.fn(),
  batchExtractEntities: vi.fn(),
  processLibrary: vi.fn(),
  getSetupStatus: vi.fn().mockResolvedValue({
    setup_completed: true,
    models_ready: true,
    models_downloading: [],
    topics_count: 1,
    telegram_configured: false,
    telegram_paired: false,
  }),
}));

const {
  fetchDashboardMetrics,
  batchProcessPapers,
  batchSummarizePapers,
  batchExtractEntities,
  processLibrary,
  getSetupStatus,
} = await import('@/lib/api');

function renderHomePage() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <HomePage />
    </MemoryRouter>,
    { queryClient },
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
    trackExternalJobMock.mockReset();
    useAuthStore.setState({
      isAuthenticated: true,
      user: { id: 1, email: 'user@example.com', role: 'user' },
      authTime: Date.now(),
    });
  });

  it('renders the dashboard heading', () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
  });

  it('does not request the admin-only setup status for a regular user', async () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);

    renderHomePage();

    await screen.findByText('Papers');
    expect(getSetupStatus).not.toHaveBeenCalled();
  });

  it('keeps the setup-status check for administrators', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      user: { id: 1, email: 'admin@example.com', role: 'admin' },
      authTime: Date.now(),
    });
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);

    renderHomePage();

    await waitFor(() => expect(getSetupStatus).toHaveBeenCalledTimes(1));
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
    expect(screen.getByText('Papers')).toBeInTheDocument();
    expect(screen.getByText('7 unread · 3 unsummarized')).toBeInTheDocument();
  });

  it('shows a degraded banner and suppresses the onboarding checklist when metrics fail', async () => {
    localStorage.clear();
    useUIStore.getState()._reset();
    vi.mocked(fetchDashboardMetrics).mockRejectedValue(new Error('Network error'));
    renderHomePage();
    expect(await screen.findByText(/Dashboard unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText('Welcome to JARVIS Research Assistant')).not.toBeInTheDocument();
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
    expect(await screen.findByText('Papers')).toBeInTheDocument();
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
    expect(await screen.findByText('Papers')).toBeInTheDocument();
    // All five tiles should show 0
    const zeros = screen.getAllByText('0');
    expect(zeros.length).toBe(5);
    // Papers subtitle shows "All caught up" when unread is 0
    expect(screen.getByText('All caught up')).toBeInTheDocument();
  });

  describe('BatchButton confirmation dialogs', () => {
    beforeEach(() => {
      vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    });

    it('describes preparation without claiming one action downloads, processes, and summarizes', async () => {
      renderHomePage();
      expect(await screen.findByText('Prepare library')).toBeInTheDocument();
      expect(screen.getByText(/Queue PDF processing and summaries/i)).toBeInTheDocument();
      expect(screen.queryByText('Analyze all new papers')).not.toBeInTheDocument();
      expect(screen.queryByText(/downloads, processes, and summarizes each one/i)).not.toBeInTheDocument();
    });

    it('does not call batchProcessPapers when user cancels confirmation', async () => {
      renderHomePage();
      const button = screen.getByRole('button', { name: /Process PDFs/i });
      await userEvent.click(button);
      expect(screen.getByText('Process library PDFs?')).toBeInTheDocument();
      expect(
        screen.getByText(
          'This will queue PDF text extraction for papers that already have local PDFs. Continue?',
        ),
      ).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
      expect(batchProcessPapers).not.toHaveBeenCalled();
    });

    it('calls batchProcessPapers when user confirms', async () => {
      vi.mocked(batchProcessPapers).mockResolvedValue({ queued: 5, total_unprocessed: 5, skipped_missing_pdf: 0, job_id: 'job-123' });
      renderHomePage();
      const button = screen.getByRole('button', { name: /Process PDFs/i });
      await userEvent.click(button);
      expect(screen.getByText('Process library PDFs?')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      await waitFor(() => expect(batchProcessPapers).toHaveBeenCalledTimes(1));
    });

    it('tracks the batch process job when the backend returns a job id', async () => {
      vi.mocked(batchProcessPapers).mockResolvedValue({ queued: 5, total_unprocessed: 5, skipped_missing_pdf: 0, job_id: 'job-123' });
      renderHomePage();
      const button = screen.getByRole('button', { name: /Process PDFs/i });
      await userEvent.click(button);
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      await waitFor(() => expect(batchProcessPapers).toHaveBeenCalledTimes(1));
      expect(await screen.findByText('Queued 5 PDFs')).toBeInTheDocument();
      expect(trackExternalJobMock).toHaveBeenCalledWith({
        jobId: 'job-123',
        kind: 'papers.batch_process',
        payload: { limit: 10 },
        status: 'queued',
      });
    });

    it('does not track a batch process job when nothing is queued', async () => {
      vi.mocked(batchProcessPapers).mockResolvedValue({ queued: 0, total_unprocessed: 0, skipped_missing_pdf: 0, job_id: null });
      renderHomePage();
      await userEvent.click(screen.getByRole('button', { name: /Process PDFs/i }));
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      expect(await screen.findByText('No PDFs to process')).toBeInTheDocument();
      expect(trackExternalJobMock).not.toHaveBeenCalled();
    });

    it('Process whole library tracks the returned durable job id', async () => {
      vi.mocked(processLibrary).mockResolvedValue({ job_id: 'job-lib', status: 'queued' });
      renderHomePage();
      const button = screen.getByRole('button', { name: /Process whole library/i });
      await userEvent.click(button);
      expect(screen.getByText('Process your whole library?')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      await waitFor(() => expect(processLibrary).toHaveBeenCalledWith(true));
      expect(await screen.findByText('Processing your library')).toBeInTheDocument();
      expect(trackExternalJobMock).toHaveBeenCalledWith({
        jobId: 'job-lib',
        kind: 'papers.process_library',
        payload: { limit: 10 },
        status: 'queued',
      });
    });

    it('Process whole library does not track a job when the library is already up to date', async () => {
      vi.mocked(processLibrary).mockResolvedValue({ job_id: null, status: 'skipped', reason: 'library_already_processed' });
      renderHomePage();
      await userEvent.click(screen.getByRole('button', { name: /Process whole library/i }));
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      expect(await screen.findByText('Library already up to date')).toBeInTheDocument();
      expect(trackExternalJobMock).not.toHaveBeenCalled();
    });
  });

  describe('Advanced disclosure', () => {
    beforeEach(() => {
      vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    });

    it('sub-step buttons are absent when disclosure is collapsed', async () => {
      renderHomePage();
      expect(await screen.findByText('Prepare library')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Summarize/i })).toBeNull();
      expect(screen.queryByRole('button', { name: /Extract Entities/i })).toBeNull();
    });

    it('expanding disclosure reveals summarization but not admin-only extraction for regular users', async () => {
      renderHomePage();
      await screen.findByText('Prepare library');
      await userEvent.click(screen.getByRole('button', { name: /Advanced/i }));
      expect(screen.getByRole('button', { name: /Summarize/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Extract Entities/i })).toBeNull();
      expect(screen.getByText(/Entity extraction is available to administrators/i)).toBeInTheDocument();
    });

    it('expanding disclosure reveals entity extraction for admins', async () => {
      useAuthStore.setState({
        isAuthenticated: true,
        user: { id: 1, email: 'admin@example.com', role: 'admin' },
        authTime: Date.now(),
      });
      renderHomePage();
      await screen.findByText('Prepare library');
      await userEvent.click(screen.getByRole('button', { name: /Advanced/i }));
      expect(screen.getByRole('button', { name: /Extract Entities/i })).toBeInTheDocument();
    });

    it('Summarize tracks the returned durable job id', async () => {
      vi.mocked(batchSummarizePapers).mockResolvedValue({ total_unsummarized: 3, job_id: 'job-sum' });
      renderHomePage();
      await screen.findByText('Prepare library');
      await userEvent.click(screen.getByRole('button', { name: /Advanced/i }));
      await userEvent.click(screen.getByRole('button', { name: /Summarize/i }));
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      await waitFor(() => expect(batchSummarizePapers).toHaveBeenCalled());
      expect(trackExternalJobMock).toHaveBeenCalledWith({
        jobId: 'job-sum',
        kind: 'papers.batch_summarize',
        payload: { limit: 10 },
        status: 'queued',
      });
    });

    it('Summarize does not track a job when nothing is queued', async () => {
      vi.mocked(batchSummarizePapers).mockResolvedValue({ total_unsummarized: 0, job_id: null });
      renderHomePage();
      await screen.findByText('Prepare library');
      await userEvent.click(screen.getByRole('button', { name: /Advanced/i }));
      await userEvent.click(screen.getByRole('button', { name: /Summarize/i }));
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      expect(await screen.findByText('No summaries to queue')).toBeInTheDocument();
      expect(trackExternalJobMock).not.toHaveBeenCalled();
    });

    it('Extract Entities calls batchExtractEntities after confirm', async () => {
      useAuthStore.setState({
        isAuthenticated: true,
        user: { id: 1, email: 'admin@example.com', role: 'admin' },
        authTime: Date.now(),
      });
      vi.mocked(batchExtractEntities).mockResolvedValue({ extracted: 4, failed: 0, total: 4 });
      renderHomePage();
      await screen.findByText('Prepare library');
      await userEvent.click(screen.getByRole('button', { name: /Advanced/i }));
      await userEvent.click(screen.getByRole('button', { name: /Extract Entities/i }));
      await userEvent.click(screen.getByRole('button', { name: /continue/i }));
      await waitFor(() => expect(batchExtractEntities).toHaveBeenCalled());
    });
  });

  describe('onboarding celebration', () => {
    const CELEBRATION_TEXT = 'All set! Happy researching.';
    // Captured before any test swaps in a spy, so beforeEach can restore it.
    const realMarkOnboardingCelebrated = useUIStore.getState().markOnboardingCelebrated;

    beforeEach(() => {
      localStorage.clear();
      useUIStore.getState()._reset();
      useUIStore.setState({ markOnboardingCelebrated: realMarkOnboardingCelebrated });
    });

    it('shows the celebration when stage is complete and not yet celebrated', async () => {
      vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
      renderHomePage();
      expect(await screen.findByText(CELEBRATION_TEXT)).toBeInTheDocument();
      // The persisted flag flips immediately so the celebration never re-fires...
      await waitFor(() => expect(useUIStore.getState().onboardingCelebrated).toBe(true));
      // ...but the latched card stays visible for the current visit.
      expect(screen.getByText(CELEBRATION_TEXT)).toBeInTheDocument();
    });

    it('calls markOnboardingCelebrated exactly once (no effect re-fire loop)', async () => {
      vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
      const spy = vi.fn(realMarkOnboardingCelebrated);
      useUIStore.setState({ markOnboardingCelebrated: spy });
      renderHomePage();
      expect(await screen.findByText(CELEBRATION_TEXT)).toBeInTheDocument();
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
      // Force an extra render pass; the effect must not fire again.
      act(() => {
        useUIStore.setState({ sidebarCollapsed: true });
      });
      expect(spy).toHaveBeenCalledTimes(1);
      expect(screen.getByText(CELEBRATION_TEXT)).toBeInTheDocument();
    });

    it('does not show the celebration when already celebrated', async () => {
      useUIStore.setState({ onboardingCelebrated: true });
      vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
      renderHomePage();
      expect(await screen.findByText('Papers')).toBeInTheDocument();
      expect(screen.queryByText(CELEBRATION_TEXT)).toBeNull();
    });

    it('does not show the celebration while onboarding is incomplete', async () => {
      vi.mocked(fetchDashboardMetrics).mockResolvedValue({
        ...mockMetrics,
        onboarding_stage: 'needs_papers',
      });
      renderHomePage();
      expect(await screen.findByText('Welcome to JARVIS Research Assistant')).toBeInTheDocument();
      expect(screen.queryByText(CELEBRATION_TEXT)).toBeNull();
    });
  });
});

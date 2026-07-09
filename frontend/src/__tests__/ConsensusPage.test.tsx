import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConsensusPage } from '@/pages/ConsensusPage';

const fetchConsensusMock = vi.fn();
const scanContradictionsMock = vi.fn().mockResolvedValue({ job_id: 'x', status: 'queued' });
vi.mock('@/lib/api', () => ({
  fetchConsensus: () => fetchConsensusMock(),
  scanContradictions: () => scanContradictionsMock(),
}));

const jobStoreMock = vi.hoisted(() => ({
  jobs: {} as Record<string, unknown>,
  trackExternalJob: vi.fn(),
  isRunning: vi.fn(() => false),
}));

vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (s: {
    jobs: Record<string, unknown>;
    trackExternalJob: typeof jobStoreMock.trackExternalJob;
    isRunning: typeof jobStoreMock.isRunning;
  }) => unknown) =>
    selector({
      jobs: jobStoreMock.jobs,
      trackExternalJob: jobStoreMock.trackExternalJob,
      isRunning: jobStoreMock.isRunning,
    }),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/consensus']}>
        <ConsensusPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchConsensusMock.mockReset();
  scanContradictionsMock.mockClear();
  jobStoreMock.jobs = {};
  jobStoreMock.trackExternalJob.mockClear();
  jobStoreMock.isRunning.mockReset();
  jobStoreMock.isRunning.mockReturnValue(false);
});

describe('ConsensusPage', () => {
  it('renders claim clusters with stance counts and click-through to verified quotes', async () => {
    fetchConsensusMock.mockResolvedValue({
      total: 1,
      claims: [
        {
          claim_topic: 'effect of X on Y',
          supports: 2,
          opposes: 1,
          paper_ids: [1, 2, 3],
          assessments: [
            {
              stance: 'supports',
              paper_a_title: 'Paper A',
              paper_b_title: 'Paper B',
              quote_a: 'A supports X',
              quote_b: 'B supports X',
              page_a: 3,
              page_b: 5,
            },
          ],
        },
      ],
    });

    renderPage();

    expect(await screen.findByText(/2 support/)).toBeInTheDocument();
    expect(screen.getByText(/1 oppose/)).toBeInTheDocument();

    // Evidence is hidden until the user expands it (click-through to the quote).
    expect(screen.queryByText(/A supports X/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /show evidence/i }));
    expect(await screen.findByText(/A supports X/)).toBeInTheDocument();
  });

  it('shows an honest empty state when there are no claims', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    renderPage();
    expect(await screen.findByText('No related-paper claims yet')).toBeInTheDocument();
  });

  it('distinguishes a completed scan that found no consensus clusters', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    jobStoreMock.jobs = {
      'job-scan': {
        id: 'job-scan',
        kind: 'contradictions.scan',
        status: 'succeeded',
        progress: 1,
        progress_message: 'Done',
        payload: {},
        result: {},
        error: null,
        created_at: '2026-07-06T10:00:00Z',
        started_at: '2026-07-06T10:00:01Z',
        finished_at: '2026-07-06T10:00:10Z',
      },
    };

    renderPage();

    expect(await screen.findByText('No consensus clusters found')).toBeInTheDocument();
    expect(screen.getByText(/scan finished/i)).toBeInTheDocument();
  });

  it('explains a succeeded scan that had no cross-referenced candidate pairs', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    jobStoreMock.jobs = {
      'job-scan': {
        id: 'job-scan',
        kind: 'contradictions.scan',
        status: 'succeeded',
        progress: 1,
        progress_message: 'Done',
        payload: {},
        result: {
          candidate_count: 0,
          contradictions_found: 0,
          contradiction_ids: [],
          llm_failures: 0,
          verification_failures: 0,
        },
        error: null,
        created_at: '2026-07-06T10:00:00Z',
        started_at: '2026-07-06T10:00:01Z',
        finished_at: '2026-07-06T10:00:10Z',
      },
    };

    renderPage();

    expect(
      await screen.findByText(/none of your processed papers are cross-referenced/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/scan finished/i)).not.toBeInTheDocument();
  });

  it('explains a succeeded scan whose candidates all failed quote verification', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    jobStoreMock.jobs = {
      'job-scan': {
        id: 'job-scan',
        kind: 'contradictions.scan',
        status: 'succeeded',
        progress: 1,
        progress_message: 'Done',
        payload: {},
        result: {
          candidate_count: 3,
          contradictions_found: 0,
          contradiction_ids: [],
          llm_failures: 0,
          verification_failures: 3,
        },
        error: null,
        created_at: '2026-07-06T10:00:00Z',
        started_at: '2026-07-06T10:00:01Z',
        finished_at: '2026-07-06T10:00:10Z',
      },
    };

    renderPage();

    expect(
      await screen.findByText(/found 3 candidate pairs; none passed quote verification/i),
    ).toBeInTheDocument();
    // Compact diagnostics line with the raw scan counts.
    expect(
      screen.getByText('3 candidate pairs · 3 verification failures · 0 verified contradictions'),
    ).toBeInTheDocument();
  });

  it('shows an actionable message when the scan is skipped for an unprocessed library', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    scanContradictionsMock.mockResolvedValueOnce({
      job_id: null,
      status: 'skipped',
      reason: 'no_findings',
    });

    renderPage();
    await userEvent.click(await screen.findByRole('button', { name: /run consensus scan/i }));

    expect(await screen.findByText(/process some papers first/i)).toBeInTheDocument();
    expect(jobStoreMock.trackExternalJob).not.toHaveBeenCalled();
  });

  it('surfaces a failed consensus scan without hiding the retry action', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    jobStoreMock.jobs = {
      'job-scan': {
        id: 'job-scan',
        kind: 'contradictions.scan',
        status: 'failed',
        progress: 0.4,
        progress_message: 'Failed',
        payload: {},
        result: null,
        error: { message: 'Model route unavailable' },
        created_at: '2026-07-06T10:00:00Z',
        started_at: '2026-07-06T10:00:01Z',
        finished_at: '2026-07-06T10:00:10Z',
      },
    };

    renderPage();

    expect(await screen.findByText('No related-paper claims yet')).toBeInTheDocument();
    expect(screen.getByText(/last contradiction scan failed/i)).toBeInTheDocument();
    expect(screen.getByText('Model route unavailable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /run consensus scan/i })).toBeInTheDocument();
  });

  it('runs a consensus scan from the empty-state CTA', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    renderPage();
    const cta = await screen.findByRole('button', { name: /run consensus scan/i });
    await userEvent.click(cta);
    expect(scanContradictionsMock).toHaveBeenCalledTimes(1);
  });

  it('does not treat paper-level scans as library-wide scan progress', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    jobStoreMock.jobs = {
      'paper-scan': {
        id: 'paper-scan',
        kind: 'contradictions.scan',
        status: 'running',
        progress: 0.5,
        progress_message: 'Scanning one paper',
        payload: { paper_id: 42, limit: 20 },
        result: null,
        error: null,
        created_at: '2026-07-06T10:00:00Z',
        started_at: '2026-07-06T10:00:01Z',
        finished_at: null,
      },
    };

    renderPage();

    expect(await screen.findByRole('button', { name: /run consensus scan/i })).toBeInTheDocument();
  });

  it('shows scanning state for a library-wide scan', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    jobStoreMock.jobs = {
      'library-scan': {
        id: 'library-scan',
        kind: 'contradictions.scan',
        status: 'queued',
        progress: 0,
        progress_message: 'Queued',
        payload: {},
        result: null,
        error: null,
        created_at: '2026-07-06T10:00:00Z',
        started_at: null,
        finished_at: null,
      },
    };

    renderPage();

    expect(await screen.findByRole('button', { name: /scanning/i })).toBeInTheDocument();
  });

  it('warns when cached claims are shown after the latest library scan failed', async () => {
    fetchConsensusMock.mockResolvedValue({
      total: 1,
      claims: [
        {
          claim_topic: 'stale claim',
          supports: 1,
          opposes: 0,
          paper_ids: [1, 2],
          assessments: [
            {
              stance: 'supports',
              paper_a_title: 'Paper A',
              paper_b_title: 'Paper B',
              quote_a: 'A supports X',
              quote_b: 'B supports X',
              page_a: null,
              page_b: null,
            },
          ],
        },
      ],
    });
    jobStoreMock.jobs = {
      'failed-library-scan': {
        id: 'failed-library-scan',
        kind: 'contradictions.scan',
        status: 'failed',
        progress: 0.5,
        progress_message: 'Failed',
        payload: {},
        result: null,
        error: { message: 'Model route unavailable' },
        created_at: '2026-07-06T10:00:00Z',
        started_at: '2026-07-06T10:00:01Z',
        finished_at: '2026-07-06T10:00:10Z',
      },
    };

    renderPage();

    expect(await screen.findByText(/1 support/)).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('The latest consensus scan failed.');
    expect(screen.getByRole('alert')).toHaveTextContent('Model route unavailable');
  });

  it('shows a degraded state when the fetch fails', async () => {
    fetchConsensusMock.mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByText(/Failed to load consensus/)).toBeInTheDocument();
  });
});

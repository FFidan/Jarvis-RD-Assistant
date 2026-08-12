import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { JobsIndicator } from '@/components/layout/JobsIndicator';
import { useJobStore } from '@/stores/job-store';
import type { Job } from '@/stores/job-store';

// Mock the job store
vi.mock('@/stores/job-store');

const mockUseJobStore = vi.mocked(useJobStore) as unknown as ReturnType<typeof vi.fn>;

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    kind: 'paper.process',
    status: 'running',
    progress: 0,
    progress_message: null,
    result: null,
    error: null,
    created_at: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:01Z',
    finished_at: null,
    ...overrides,
  };
}

function setupStore(jobs: Record<string, Job>) {
  const cancelJob = vi.fn();
  const removeJob = vi.fn();
  mockUseJobStore.mockImplementation((selector: (s: unknown) => unknown) => {
    const state = { jobs, cancelJob, removeJob };
    return selector(state);
  });
}

describe('JobsIndicator', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing when there are no jobs', () => {
    setupStore({});
    const { container } = render(<JobsIndicator />);
    expect(container.firstChild).toBeNull();
  });

  it('scales progress from 0-1 to 0-100 for the Progress bar', async () => {
    const job = makeJob({ progress: 0.5 });
    setupStore({ 'job-1': job });

    render(<JobsIndicator />);

    // Open the popover to render job rows
    const trigger = screen.getByRole('button', { name: /background tasks/i });
    await userEvent.click(trigger);

    // The Shadcn Progress indicator uses inline transform to show fill.
    // With value=50 (after scaling 0.5 * 100), the transform is translateX(-50%).
    // Find the progress indicator div by its inline style.
    const indicators = document.querySelectorAll('[style*="translateX"]');
    expect(indicators.length).toBeGreaterThan(0);

    // The transform for a 50% filled bar should be translateX(-50%)
    const indicator = indicators[0] as HTMLElement;
    expect(indicator.style.transform).toBe('translateX(-50%)');
  });

  it('labels a library-wide contradiction scan job', async () => {
    setupStore({
      'job-contradictions': makeJob({
        id: 'job-contradictions',
        kind: 'contradictions.scan',
        payload: {},
      }),
    });

    render(<JobsIndicator />);

    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Scanning Contradictions')).toBeInTheDocument();
  });

  it('labels a paper-scoped contradiction scan job distinctly from the library-wide scan', async () => {
    setupStore({
      'job-contradictions-paper': makeJob({
        id: 'job-contradictions-paper',
        kind: 'contradictions.scan',
        payload: { paper_id: 42 },
      }),
    });

    render(<JobsIndicator />);

    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Scanning Paper Contradictions')).toBeInTheDocument();
    expect(screen.queryByText('Scanning Contradictions')).toBeNull();
  });

  it('labels a partially completed whole-library job and exposes its incomplete counts', async () => {
    setupStore({
      'job-lib': makeJob({
        id: 'job-lib',
        kind: 'papers.process_library',
        status: 'succeeded',
        result: {
          status: 'partial', total: 5, downloaded: 1, processed: 2, summarized: 0,
          blocked: [{ paper_id: 5, reason: 'no_pdf_source' }],
          errors: [{ paper_id: 4, stage: 'process', error: 'boom' }],
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Partial')).toBeInTheDocument();
    expect(screen.queryByText('Done')).toBeNull();
    expect(
      screen.getByRole('status', { name: 'Incomplete: 1 failed, 1 skipped of 5' }),
    ).toBeInTheDocument();
  });

  it('renders the blocked-only partial line (no failures)', async () => {
    setupStore({
      'job-lib2': makeJob({
        id: 'job-lib2',
        kind: 'papers.process_library',
        status: 'succeeded',
        result: {
          status: 'partial', total: 2, downloaded: 0, processed: 0, summarized: 0,
          blocked: [
            { paper_id: 10, reason: 'no_pdf_source' },
            { paper_id: 11, reason: 'no_pdf_source' },
          ],
          errors: [],
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('0 failed, 2 skipped of 2')).toBeInTheDocument();
  });

  it('renders canonical Zotero partial counts', async () => {
    setupStore({
      'job-zotero': makeJob({
        id: 'job-zotero',
        kind: 'zotero.poll',
        status: 'succeeded',
        result: {
          status: 'partial', new_items: 22, linked: 3, enqueued: 18,
          parse_failed: 1, ingest_failed: 1, gave_up: 0, capped: true,
          failed: 2, skipped: 0, remaining: 2, total: 24,
          version_from: 10, version_to: 10, cursor_persisted: true,
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(
      screen.getByRole('status', { name: 'Incomplete: 2 failed, 0 skipped, 2 not processed of 24' }),
    ).toBeInTheDocument();
  });

  it('uses a generic message when a partial has no countable outcome', async () => {
    setupStore({
      'job-zotero-cursor': makeJob({
        id: 'job-zotero-cursor',
        kind: 'zotero.poll',
        status: 'succeeded',
        result: {
          status: 'partial', new_items: 1, linked: 0, enqueued: 0,
          parse_failed: 0, ingest_failed: 0, gave_up: 0, capped: false,
          failed: 0, skipped: 0, remaining: 0, total: 1,
          version_from: 10, version_to: 11, cursor_persisted: false,
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(
      screen.getByRole('status', { name: 'Incomplete: Details unavailable' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('0 failed, 0 skipped of 1')).toBeNull();
  });

  it('a cancelled whole-library result reads Cancelled, never Done', async () => {
    setupStore({
      'job-libcancel': makeJob({
        id: 'job-libcancel',
        kind: 'papers.process_library',
        status: 'succeeded',
        result: {
          status: 'cancelled', total: 100, downloaded: 0, processed: 3, summarized: 0,
          blocked: [], errors: [],
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.queryByText('Done')).toBeNull();
  });

  it('a cancelled whole-library result still shows the failures accrued before the stop', async () => {
    setupStore({
      'job-libcancelerr': makeJob({
        id: 'job-libcancelerr',
        kind: 'papers.process_library',
        status: 'succeeded',
        result: {
          status: 'cancelled', total: 100, remaining: 97, downloaded: 0, processed: 3, summarized: 0,
          blocked: [{ paper_id: 9, reason: 'no_pdf_source' }],
          errors: [
            { paper_id: 7, stage: 'process', error: 'boom' },
            { paper_id: 8, stage: 'process', error: 'boom' },
          ],
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.getByText('2 failed, 1 skipped, 97 not processed of 100')).toBeInTheDocument();
  });

  it('shows untouched papers when cancellation stops a library run early', async () => {
    setupStore({
      'job-libcancelremaining': makeJob({
        id: 'job-libcancelremaining',
        kind: 'papers.process_library',
        status: 'succeeded',
        result: {
          status: 'cancelled', total: 3, examined: 2, remaining: 1,
          downloaded: 0, processed: 2, summarized: 0, blocked: [], errors: [],
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('0 failed, 0 skipped, 1 not processed of 3')).toBeInTheDocument();
  });

  it('shows scalar batch counts instead of re-deriving zeroes from absent arrays', async () => {
    setupStore({
      'job-extract-partial': makeJob({
        id: 'job-extract-partial',
        kind: 'extraction.batch',
        status: 'succeeded',
        result: {
          status: 'partial', total: 5, extracted: 2, failed: 2, skipped: 1, remaining: 0,
        },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('2 failed, 1 skipped of 5')).toBeInTheDocument();
    expect(screen.queryByText('0 failed, 0 skipped of 5')).toBeNull();
  });

  it('a cancel-requested job reads Cancelling and disables its cancel control', async () => {
    // The handler keeps running until it observes the flag, so status stays
    // `running`. Without a distinct display state the row would look untouched
    // and the click would seem not to have registered.
    setupStore({
      'job-cancelling': makeJob({
        id: 'job-cancelling',
        kind: 'papers.process_library',
        status: 'running',
        cancel_requested: true,
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Cancelling…')).toBeInTheDocument();
    expect(screen.queryByText('Running')).toBeNull();
    // Idempotent by construction: the control cannot be clicked again.
    expect(screen.getByRole('button', { name: /cancellation requested/i })).toBeDisabled();
  });

  it('a plain running job reads Running with an enabled cancel control', async () => {
    // Negative control for the test above — the cancelling state must not leak
    // onto ordinary running jobs.
    setupStore({
      'job-plain': makeJob({ id: 'job-plain', status: 'running' }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.queryByText('Cancelling…')).toBeNull();
    expect(screen.getByRole('button', { name: /cancel job/i })).toBeEnabled();
  });

  it('a terminal cancelled job reads Cancelled, not Cancelling', async () => {
    // The flag stays set on the final row; once the job is terminal the outcome
    // must win over the request.
    setupStore({
      'job-done-cancel': makeJob({
        id: 'job-done-cancel',
        status: 'cancelled',
        cancel_requested: true,
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.queryByText('Cancelling…')).toBeNull();
  });

  it('a plain-ok succeeded job stays green Done with no partial line', async () => {
    setupStore({
      'job-ok': makeJob({
        id: 'job-ok',
        kind: 'papers.process_library',
        status: 'succeeded',
        result: { status: 'ok', total: 2, downloaded: 0, processed: 2, summarized: 0, blocked: [], errors: [] },
      }),
    });

    render(<JobsIndicator />);
    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Done')).toBeInTheDocument();
    expect(screen.queryByText(/skipped of/i)).toBeNull();
  });
});

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

  it('labels contradiction scan jobs', async () => {
    setupStore({
      'job-contradictions': makeJob({
        id: 'job-contradictions',
        kind: 'contradictions.scan',
      }),
    });

    render(<JobsIndicator />);

    await userEvent.click(screen.getByRole('button', { name: /background tasks/i }));

    expect(screen.getByText('Scanning Contradictions')).toBeInTheDocument();
  });

  it('renders an amber partial line for a succeeded whole-library job (status still Done)', async () => {
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

    expect(screen.getByText('1 failed, 1 skipped of 5')).toBeInTheDocument();
    // The status pill still reads Done — partial detail is additive, not a failure.
    expect(screen.getByText('Done')).toBeInTheDocument();
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

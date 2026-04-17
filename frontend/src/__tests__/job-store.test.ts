/**
 * Tests for the job store (job-store.ts).
 *
 * Strategy:
 * - Mock fetch globally with vi.spyOn so tests control both REST responses
 *   and SSE streams.
 * - Mock sonner so toast calls are observable without a DOM.
 * - Reset store state before each test via setState.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useJobStore, type Job } from '@/stores/job-store';

// --- Module mocks (hoisted before any imports) ---

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => 'test-key'),
      logout: vi.fn(),
    })),
  },
}));

// createJob and listJobs/cancelJob are used in the store — mock the whole module
vi.mock('@/lib/api', () => ({
  createJob: vi.fn(),
  listJobs: vi.fn(),
  cancelJob: vi.fn(),
}));

// --- Helpers ---

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    kind: 'pulse.generate',
    status: 'queued',
    progress: 0,
    progress_message: null,
    result: null,
    error: null,
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function createMockSSEStream(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let idx = 0;
  return new ReadableStream({
    pull(controller) {
      if (idx < frames.length) {
        controller.enqueue(encoder.encode(frames[idx]));
        idx++;
      } else {
        controller.close();
      }
    },
  });
}

// --- Tests ---

describe('JobStore', () => {
  beforeEach(() => {
    // Reset store to empty state
    useJobStore.setState({ jobs: {}, activeAborts: {} });
    vi.restoreAllMocks();
    // Re-stub sonner after restoreAllMocks
    vi.mock('sonner', () => ({
      toast: { success: vi.fn(), error: vi.fn() },
    }));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // ----- startJob -----

  it('startJob: POSTs via createJob and adds placeholder job with status queued', async () => {
    const { createJob } = await import('@/lib/api');
    vi.mocked(createJob).mockResolvedValue({ job_id: 'job-abc', status: 'queued' });

    // Return an empty SSE stream so subscribe doesn't block
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(createMockSSEStream([]), { status: 200 }),
    );

    const jobId = await useJobStore.getState().startJob('pulse.generate', { foo: 'bar' });

    expect(jobId).toBe('job-abc');
    expect(createJob).toHaveBeenCalledWith('pulse.generate', { foo: 'bar' });

    const job = useJobStore.getState().jobs['job-abc'];
    expect(job).toBeDefined();
    expect(job.status).toBe('queued');
    expect(job.kind).toBe('pulse.generate');
  });

  // ----- subscribe / SSE progress events -----

  it('subscribe: SSE progress events update store progress and message', async () => {
    // Pre-populate a job
    const job = makeJob({ id: 'job-2', status: 'queued' });
    useJobStore.setState({ jobs: { 'job-2': job }, activeAborts: {} });

    const progressEvent = JSON.stringify({
      status: 'running',
      progress: 42,
      progress_message: 'Processing chunk 4/10',
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          `data: ${progressEvent}\n\n`,
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().subscribe('job-2');

    // Wait for async SSE processing to complete
    await new Promise((r) => setTimeout(r, 50));

    const updated = useJobStore.getState().jobs['job-2'];
    expect(updated.status).toBe('running');
    expect(updated.progress).toBe(42);
    expect(updated.progress_message).toBe('Processing chunk 4/10');
  });

  // ----- terminal events -----

  it('subscribe: succeeded terminal event fires toast.success and keeps job in store', async () => {
    const { toast } = await import('sonner');

    const job = makeJob({ id: 'job-3', status: 'running' });
    useJobStore.setState({ jobs: { 'job-3': job }, activeAborts: {} });

    const doneEvent = JSON.stringify({
      status: 'succeeded',
      progress: 100,
      progress_message: null,
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([`data: ${doneEvent}\n\n`]),
        { status: 200 },
      ),
    );

    useJobStore.getState().subscribe('job-3');

    // Wait for async SSE processing (real timers — no fake timers here to avoid
    // accidentally firing the 5-min eviction timer during the assertion window)
    await new Promise((r) => setTimeout(r, 50));

    // Job should still be in store (eviction timer hasn't fired yet)
    expect(useJobStore.getState().jobs['job-3']).toBeDefined();
    expect(useJobStore.getState().jobs['job-3'].status).toBe('succeeded');
    expect(toast.success).toHaveBeenCalledWith('pulse.generate completed');
  });

  it('subscribe: failed terminal event fires toast.error with message', async () => {
    const { toast } = await import('sonner');

    const job = makeJob({ id: 'job-4', status: 'running' });
    useJobStore.setState({ jobs: { 'job-4': job }, activeAborts: {} });

    const failEvent = JSON.stringify({
      status: 'failed',
      progress: 10,
      error: { message: 'LLM timeout after 30s' },
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([`data: ${failEvent}\n\n`]),
        { status: 200 },
      ),
    );

    useJobStore.getState().subscribe('job-4');
    await new Promise((r) => setTimeout(r, 50));

    expect(toast.error).toHaveBeenCalledWith('LLM timeout after 30s');
    expect(useJobStore.getState().jobs['job-4'].status).toBe('failed');
  });

  // ----- hasRunning -----

  it('hasRunning: returns true when a job of that kind is queued', () => {
    useJobStore.setState({
      jobs: { 'j1': makeJob({ kind: 'pulse.generate', status: 'queued' }) },
      activeAborts: {},
    });
    expect(useJobStore.getState().hasRunning('pulse.generate')).toBe(true);
    expect(useJobStore.getState().hasRunning('card.generate')).toBe(false);
  });

  it('hasRunning: returns true when a job of that kind is running', () => {
    useJobStore.setState({
      jobs: { 'j2': makeJob({ kind: 'paper.process', status: 'running' }) },
      activeAborts: {},
    });
    expect(useJobStore.getState().hasRunning('paper.process')).toBe(true);
  });

  it('hasRunning: returns false for succeeded/failed/cancelled jobs', () => {
    useJobStore.setState({
      jobs: {
        'j3': makeJob({ kind: 'pulse.generate', status: 'succeeded' }),
        'j4': makeJob({ id: 'j4', kind: 'pulse.generate', status: 'failed' }),
        'j5': makeJob({ id: 'j5', kind: 'pulse.generate', status: 'cancelled' }),
      },
      activeAborts: {},
    });
    expect(useJobStore.getState().hasRunning('pulse.generate')).toBe(false);
  });

  // ----- cancelJob -----

  it('cancelJob: calls API and optimistically sets status to cancelled', async () => {
    const { cancelJob: apiCancelJob } = await import('@/lib/api');
    vi.mocked(apiCancelJob).mockResolvedValue(undefined);

    vi.useFakeTimers();

    useJobStore.setState({
      jobs: { 'j6': makeJob({ id: 'j6', kind: 'pulse.generate', status: 'running' }) },
      activeAborts: {},
    });

    await useJobStore.getState().cancelJob('j6');

    expect(apiCancelJob).toHaveBeenCalledWith('j6');
    expect(useJobStore.getState().jobs['j6'].status).toBe('cancelled');
  });

  // ----- removeJob -----

  it('removeJob: removes the job from store immediately', () => {
    useJobStore.setState({
      jobs: { 'j7': makeJob({ id: 'j7' }) },
      activeAborts: {},
    });
    useJobStore.getState().removeJob('j7');
    expect(useJobStore.getState().jobs['j7']).toBeUndefined();
  });

  // ----- hydrate -----

  it('hydrate: re-subscribes to running jobs from API', async () => {
    const { listJobs } = await import('@/lib/api');
    const runningJob = makeJob({ id: 'j8', status: 'running' });
    vi.mocked(listJobs).mockResolvedValue([runningJob]);

    // Return empty stream so subscribe terminates cleanly
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(createMockSSEStream([]), { status: 200 }),
    );

    await useJobStore.getState().hydrate();

    expect(listJobs).toHaveBeenCalledWith({ status: 'running' });
    expect(useJobStore.getState().jobs['j8']).toBeDefined();
    expect(useJobStore.getState().jobs['j8'].status).toBe('running');
  });

  it('hydrate: does not crash when API returns error', async () => {
    const { listJobs } = await import('@/lib/api');
    vi.mocked(listJobs).mockRejectedValue(new Error('Network error'));

    await expect(useJobStore.getState().hydrate()).resolves.not.toThrow();
  });
});

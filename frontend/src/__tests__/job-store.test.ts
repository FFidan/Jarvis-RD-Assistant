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
import { queryClient } from '@/lib/query-client';

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
  getJob: vi.fn(),
}));

// --- Helpers ---

function requireJob(job: Job | undefined, label = 'job'): Job {
  if (!job) throw new Error(`test fixture: ${label} not found in store`);
  return job;
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    kind: 'pulse.generate',
    status: 'queued',
    progress: 0,
    progress_message: null,
    payload: {},
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

    const job = requireJob(useJobStore.getState().jobs['job-abc'], 'job-abc');
    expect(job.status).toBe('queued');
    expect(job.kind).toBe('pulse.generate');
  });

  it.each(['zotero.push', 'zotero.resync'] as const)(
    'trackExternalJob: invalidates zotero linkage queries when %s succeeds',
    async (kind) => {
      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      vi.spyOn(globalThis, 'fetch').mockResolvedValue(
        new Response(
          createMockSSEStream([
            'data: {"status":"running","progress":25,"progress_message":"Working"}\n\n',
            'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
            'data: [DONE]\n\n',
          ]),
          { status: 200 },
        ),
      );

      useJobStore.getState().trackExternalJob({
        jobId: `job-${kind}`,
        kind,
        payload: { paper_id: 77 },
        status: 'queued',
      });

      expect(useJobStore.getState().jobs[`job-${kind}`]).toMatchObject({
        kind,
        status: 'queued',
        payload: { paper_id: 77 },
      });

      await new Promise((r) => setTimeout(r, 50));

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['zotero-linkage', 77] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['paper-detail', 77] });
    },
  );

  it('trackExternalJob: invalidates zotero-library query when zotero.poll succeeds', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":25,"progress_message":"Polling"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-zotero-poll',
      kind: 'zotero.poll',
      payload: {},
      status: 'queued',
    });

    expect(useJobStore.getState().jobs['job-zotero-poll']).toMatchObject({
      kind: 'zotero.poll',
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['zotero-library'] });
  });

  it('trackExternalJob: invalidates contradiction queries when contradiction scan succeeds', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":0.25,"progress_message":"Scanning"}\n\n',
          'data: {"status":"succeeded","progress":1,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-contradictions',
      kind: 'contradictions.scan',
      payload: { paper_id: 77, limit: 20 },
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['contradictions', 77, 'verified'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['paper-detail', 77] });
  });

  it('subscribe: running + [DONE] reconciles and retries external Zotero jobs', async () => {
    vi.useFakeTimers();
    const { getJob } = await import('@/lib/api');

    vi.mocked(getJob)
      .mockResolvedValueOnce(
        makeJob({
          id: 'job-zotero-transient',
          kind: 'zotero.push',
          status: 'running',
          progress: 20,
          progress_message: 'Still working',
          payload: { paper_id: 77 },
        }),
      )
      .mockResolvedValueOnce(
        makeJob({
          id: 'job-zotero-transient',
          kind: 'zotero.push',
          status: 'succeeded',
          progress: 100,
          progress_message: 'Done',
          payload: { paper_id: 77 },
        }),
      );

    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(
          createMockSSEStream([
            'data: {"status":"running","progress":20,"progress_message":"Still working"}\n\n',
            'data: [DONE]\n\n',
          ]),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          createMockSSEStream([
            'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
            'data: [DONE]\n\n',
          ]),
          { status: 200 },
        ),
      );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-zotero-transient',
      kind: 'zotero.push',
      payload: { paper_id: 77 },
      status: 'queued',
    });

    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(0);

    expect(getJob).toHaveBeenCalledWith('job-zotero-transient');
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    expect(requireJob(useJobStore.getState().jobs['job-zotero-transient'], 'job-zotero-transient').status).toBe('succeeded');
    expect(useJobStore.getState().activeAborts['job-zotero-transient']).toBeUndefined();
  });

  it.each([401, 403] as const)(
    'subscribe: %s SSE response clears auth-failed external Zotero jobs from busy state',
    async (statusCode) => {
      const { useAuthStore } = await import('@/stores/auth-store');
      const logout = vi.fn();
      vi.mocked(useAuthStore.getState).mockReturnValue({
        getApiKey: vi.fn(() => 'test-key'),
        getUser: vi.fn(() => null),
        logout,
        isAuthenticated: true,
        authTime: null,
        apiKey: 'test-key',
        user: null,
        login: vi.fn(),
        loginWithSession: vi.fn(),
        checkSession: vi.fn(() => true),
      });

      vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: statusCode }));

      useJobStore.getState().trackExternalJob({
        jobId: 'job-zotero-auth',
        kind: 'zotero.push',
        payload: { paper_id: 88 },
        status: 'queued',
      });

      await new Promise((r) => setTimeout(r, 50));

      expect(logout).toHaveBeenCalledTimes(1);
      expect(useJobStore.getState().jobs['job-zotero-auth']).toBeUndefined();
      expect(useJobStore.getState().activeAborts['job-zotero-auth']).toBeUndefined();
    },
  );

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

    const updated = requireJob(useJobStore.getState().jobs['job-2'], 'job-2');
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
    expect(requireJob(useJobStore.getState().jobs['job-3'], 'job-3').status).toBe('succeeded');
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
    expect(requireJob(useJobStore.getState().jobs['job-4'], 'job-4').status).toBe('failed');
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
    expect(requireJob(useJobStore.getState().jobs['j6'], 'j6').status).toBe('cancelled');
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
    // hydrate now calls listJobs twice (running + queued)
    vi.mocked(listJobs)
      .mockResolvedValueOnce([runningJob]) // running call
      .mockResolvedValueOnce([]);           // queued call

    // Return empty stream so subscribe terminates cleanly
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(createMockSSEStream([]), { status: 200 }),
    );

    await useJobStore.getState().hydrate();

    expect(listJobs).toHaveBeenCalledWith({ status: 'running' });
    expect(useJobStore.getState().jobs['j8']).toBeDefined();
    expect(requireJob(useJobStore.getState().jobs['j8'], 'j8').status).toBe('running');
  });

  it('hydrate: does not crash when API returns error', async () => {
    const { listJobs } = await import('@/lib/api');
    vi.mocked(listJobs).mockRejectedValue(new Error('Network error'));

    await expect(useJobStore.getState().hydrate()).resolves.not.toThrow();
  });

  // ----- action_link open-redirect guard (FE-004) -----

  /**
   * Helper: fire a 'failed' SSE event with an action_link and return
   * the onClick handler captured from the toast.error call.
   */
  async function captureActionLinkClick(href: string): Promise<() => void> {
    const { toast } = await import('sonner');
    const toastError = vi.mocked(toast.error);
    toastError.mockClear();

    const job = makeJob({ id: 'job-action', status: 'running' });
    useJobStore.setState({ jobs: { 'job-action': job }, activeAborts: {} });

    const failEvent = JSON.stringify({
      status: 'failed',
      error: { message: 'Something failed', action_link: { href, label: 'Retry' } },
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([`data: ${failEvent}\n\n`]),
        { status: 200 },
      ),
    );

    useJobStore.getState().subscribe('job-action');
    await new Promise((r) => setTimeout(r, 50));

    expect(toastError).toHaveBeenCalled();
    const firstCall = toastError.mock.calls[0];
    if (!firstCall) throw new Error('test fixture: toastError was not called');
    const callArg = firstCall[1] as { action?: { onClick: () => void } };
    return callArg.action!.onClick;
  }

  it('test_action_link_relative_path_navigates: relative href sets window.location.href', async () => {
    const hrefSetter = vi.fn();
    const originalDescriptor = Object.getOwnPropertyDescriptor(window, 'location');
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...window.location, set href(v: string) { hrefSetter(v); } },
    });

    try {
      const onClick = await captureActionLinkClick('/papers/1');
      onClick();
      expect(hrefSetter).toHaveBeenCalledWith('/papers/1');
    } finally {
      if (originalDescriptor) {
        Object.defineProperty(window, 'location', originalDescriptor);
      }
    }
  });

  it('test_action_link_external_url_blocked: absolute URL does NOT navigate, console.warn called', async () => {
    const hrefSetter = vi.fn();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const originalDescriptor = Object.getOwnPropertyDescriptor(window, 'location');
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...window.location, set href(v: string) { hrefSetter(v); } },
    });

    try {
      const onClick = await captureActionLinkClick('https://evil.com');
      onClick();
      expect(hrefSetter).not.toHaveBeenCalled();
      expect(warnSpy).toHaveBeenCalledWith(
        'Refusing non-relative action_link:',
        'https://evil.com',
      );
    } finally {
      if (originalDescriptor) {
        Object.defineProperty(window, 'location', originalDescriptor);
      }
      warnSpy.mockRestore();
    }
  });

  it('test_action_link_protocol_relative_blocked: protocol-relative URL does NOT navigate', async () => {
    const hrefSetter = vi.fn();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const originalDescriptor = Object.getOwnPropertyDescriptor(window, 'location');
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...window.location, set href(v: string) { hrefSetter(v); } },
    });

    try {
      const onClick = await captureActionLinkClick('//evil.com');
      onClick();
      expect(hrefSetter).not.toHaveBeenCalled();
      expect(warnSpy).toHaveBeenCalledWith(
        'Refusing non-relative action_link:',
        '//evil.com',
      );
    } finally {
      if (originalDescriptor) {
        Object.defineProperty(window, 'location', originalDescriptor);
      }
      warnSpy.mockRestore();
    }
  });

  it('test_action_link_javascript_blocked: javascript: URI does NOT navigate', async () => {
    const hrefSetter = vi.fn();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const originalDescriptor = Object.getOwnPropertyDescriptor(window, 'location');
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...window.location, set href(v: string) { hrefSetter(v); } },
    });

    try {
      const onClick = await captureActionLinkClick('javascript:alert(1)');
      onClick();
      expect(hrefSetter).not.toHaveBeenCalled();
      expect(warnSpy).toHaveBeenCalledWith(
        'Refusing non-relative action_link:',
        'javascript:alert(1)',
      );
    } finally {
      if (originalDescriptor) {
        Object.defineProperty(window, 'location', originalDescriptor);
      }
      warnSpy.mockRestore();
    }
  });

  it('test_hydrate_resubscribes_queued: hydrate picks up both running and queued jobs', async () => {
    const { listJobs } = await import('@/lib/api');
    const jobA_running = makeJob({ id: 'job-running-1', kind: 'pulse.generate', status: 'running' });
    const jobB_queued = makeJob({ id: 'job-queued-1', kind: 'paper.process', status: 'queued' });

    // listJobs called twice: first for running, then for queued
    vi.mocked(listJobs)
      .mockResolvedValueOnce([jobA_running]) // status: 'running'
      .mockResolvedValueOnce([jobB_queued]); // status: 'queued'

    // Return empty stream so subscribe terminates cleanly for both jobs
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(createMockSSEStream([]), { status: 200 }),
    );

    await useJobStore.getState().hydrate();

    // Both calls must have been made
    expect(listJobs).toHaveBeenCalledWith({ status: 'running' });
    expect(listJobs).toHaveBeenCalledWith({ status: 'queued' });
    expect(listJobs).toHaveBeenCalledTimes(2);

    // Both jobs must appear in the store
    const jobs = useJobStore.getState().jobs;
    expect(jobs['job-running-1']).toBeDefined();
    expect(requireJob(jobs['job-running-1'], 'job-running-1').status).toBe('running');
    expect(jobs['job-queued-1']).toBeDefined();
    expect(requireJob(jobs['job-queued-1'], 'job-queued-1').status).toBe('queued');
  });
});

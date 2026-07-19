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
import * as sseReader from '@/lib/sse-reader';
import { QUERY_KEYS } from '@/lib/query-keys';

// --- Module mocks (hoisted before any imports) ---

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  },
}));

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => 'test-key'),
      isAuthenticated: true,
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
    // resetAllMocks clears call history AND queued return values (mockResolvedValueOnce
    // etc.) on all vi.fn() instances — prevents bleed-over between tests.
    // In vitest 4, vi.clearAllMocks() no longer clears the mockResolvedValueOnce queue,
    // so vi.resetAllMocks() is required here to prevent cross-test mock queue bleed.
    vi.resetAllMocks();
    vi.restoreAllMocks();
    // Re-stub sonner after restoreAllMocks
    vi.mock('sonner', () => ({
      toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
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

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.zotero.linkage(77) });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.papers.detail(77) });
    },
  );

  it.each(['zotero.poll', 'zotero.sync_from_zotero'] as const)(
    'trackExternalJob: invalidates papers-feed and feed-counts (not dead zotero-library) when %s succeeds',
    async (kind) => {
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
        jobId: `job-${kind}`,
        kind,
        payload: {},
        status: 'queued',
      });

      await new Promise((r) => setTimeout(r, 50));

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.papers.feedAll() });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.feed.counts() });
      // Must NOT invalidate the dead key
      const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
      expect(keys).not.toContain('zotero-library');
    },
  );

  it('papers.batch_process: invalidates papers-feed, feed-counts, action-items-unprocessed on success', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":50,"progress_message":"Processing"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-batch-process',
      kind: 'papers.batch_process',
      payload: {},
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.papers.feedAll() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.feed.counts() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.actionItems.unprocessed() });
    // Must NOT invalidate dead key
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).not.toContain('papers');
  });

  it('papers.batch_summarize: invalidates papers-feed (not dead papers key) on success', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":50,"progress_message":"Summarizing"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-batch-summarize',
      kind: 'papers.batch_summarize',
      payload: {},
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.papers.feedAll() });
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).not.toContain('papers');
  });

  it('extraction.batch: invalidates extraction-table (not dead extractions key) on success', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":50,"progress_message":"Extracting"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-extraction-batch',
      kind: 'extraction.batch',
      payload: {},
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    // Note: bare prefix for invalidation — mirrors production job-store.ts (no registry factory for all-extraction-table entries; QUERY_KEYS.extraction.table requires both templateId and paperIds args)
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['extraction-table'] });
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).not.toContain('extractions');
  });

  it('pulse.generate: invalidates pulse-today and pulse-stats (not dead pulse-history) on success', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":50,"progress_message":"Generating"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-pulse-generate',
      kind: 'pulse.generate',
      payload: {},
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.pulse.today() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.pulse.statsAll() });
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).not.toContain('pulse-history');
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

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.contradictions.verified(77) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.papers.detail(77) });
  });

  it('trackExternalJob: library-wide contradiction scan (no paper_id) invalidates consensus', async () => {
    // The ConsensusPage CTA scans the whole library (no paper_id) → the paperId==null
    // branch must invalidate the consensus list so the page refetches. Dropping
    // QUERY_KEYS.consensus.all() from that branch makes this test fail.
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
      jobId: 'job-contradictions-library',
      kind: 'contradictions.scan',
      payload: { limit: 25 }, // no paper_id → library-wide branch
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.consensus.all() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['contradictions'] });
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

  it('subscribe: 401 SSE response drops the job and logs out (session invalid)', async () => {
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
      lastError: null,
      login: vi.fn(),
      loginWithSession: vi.fn(),
      isSessionValid: vi.fn(() => true),
      expireSession: vi.fn(),
      hydrateFromCookie: vi.fn(),
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 401 }));

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
  });

  it('subscribe: 403 SSE drops the job but does NOT log out (permission error, not session-invalid)', async () => {
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
      lastError: null,
      login: vi.fn(),
      loginWithSession: vi.fn(),
      isSessionValid: vi.fn(() => true),
      expireSession: vi.fn(),
      hydrateFromCookie: vi.fn(),
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 403 }));

    useJobStore.getState().trackExternalJob({
      jobId: 'job-403-no-logout',
      kind: 'zotero.push',
      payload: { paper_id: 99 },
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    // Job must be removed (permission error is unrecoverable for this job)
    expect(useJobStore.getState().jobs['job-403-no-logout']).toBeUndefined();
    expect(useJobStore.getState().activeAborts['job-403-no-logout']).toBeUndefined();
    // 403 = permission denied (authenticated user, no access). Must NOT log out.
    expect(logout).not.toHaveBeenCalled();
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
    expect(toast.success).toHaveBeenCalledWith('Generating Pulse completed');
  });

  it('subscribe: succeeded card.generate with zero cards does NOT fire the success toast', async () => {
    const { toast } = await import('sonner');

    const job = makeJob({ id: 'job-0c', kind: 'card.generate', status: 'running' });
    useJobStore.setState({ jobs: { 'job-0c': job }, activeAborts: {} });

    const doneEvent = JSON.stringify({
      status: 'succeeded',
      progress: 100,
      progress_message: null,
      result: { cards_created: 0, confidence: 'LOW' },
    });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([`data: ${doneEvent}\n\n`]),
        { status: 200 },
      ),
    );

    useJobStore.getState().subscribe('job-0c');
    await new Promise((r) => setTimeout(r, 50));

    expect(requireJob(useJobStore.getState().jobs['job-0c'], 'job-0c').status).toBe('succeeded');
    expect(toast.success).not.toHaveBeenCalled();
  });

  // ----- papers.process_library: invalidation + partial honesty -----

  function runLibraryTerminal(id: string, result: Record<string, unknown>) {
    const job = makeJob({ id, kind: 'papers.process_library', status: 'running' });
    useJobStore.setState({ jobs: { [id]: job }, activeAborts: {} });
    const doneEvent = JSON.stringify({
      status: 'succeeded', progress: 100, progress_message: 'Done', result,
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(createMockSSEStream([`data: ${doneEvent}\n\n`]), { status: 200 }),
    );
    useJobStore.getState().subscribe(id);
    return new Promise((r) => setTimeout(r, 50));
  }

  it('papers.process_library: invalidates papers-feed, feed-counts, action-items on success', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    await runLibraryTerminal('job-lib', {
      status: 'ok', total: 3, downloaded: 1, processed: 3, summarized: 0, blocked: [], errors: [],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.papers.feedAll() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.feed.counts() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.actionItems.unprocessed() });
  });

  it('papers.process_library: ok result fires toast.success, never warning', async () => {
    const { toast } = await import('sonner');
    await runLibraryTerminal('job-libok', {
      status: 'ok', total: 2, downloaded: 0, processed: 2, summarized: 0, blocked: [], errors: [],
    });
    expect(toast.success).toHaveBeenCalledWith('Whole-library processing completed');
    expect(toast.warning).not.toHaveBeenCalled();
  });

  it('papers.process_library: partial result warns naming failed + skipped, never success', async () => {
    const { toast } = await import('sonner');
    await runLibraryTerminal('job-libpart', {
      status: 'partial', total: 5, downloaded: 1, processed: 2, summarized: 0,
      blocked: [{ paper_id: 5, reason: 'no_pdf_source' }],
      errors: [{ paper_id: 4, stage: 'process', error: 'boom' }],
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalledTimes(1);
    const warnCall = vi.mocked(toast.warning).mock.calls[0];
    if (!warnCall) throw new Error('test fixture: toast.warning was not called');
    const msg = warnCall[0] as string;
    expect(msg).toContain('1 failed');
    expect(msg).toContain('1 skipped');
    expect(msg).toContain('of 5');
  });

  it('papers.process_library: blocked-only partial warns (never a green success)', async () => {
    const { toast } = await import('sonner');
    await runLibraryTerminal('job-libblock', {
      status: 'partial', total: 2, downloaded: 0, processed: 0, summarized: 0,
      blocked: [
        { paper_id: 10, reason: 'no_pdf_source' },
        { paper_id: 11, reason: 'no_pdf_source' },
      ],
      errors: [],
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalledTimes(1);
    const warnCall = vi.mocked(toast.warning).mock.calls[0];
    if (!warnCall) throw new Error('test fixture: toast.warning was not called');
    const msg = warnCall[0] as string;
    expect(msg).toContain('2 skipped');
    expect(msg).not.toContain('failed');
  });

  it('papers.process_library: cancelled result warns (never a green success)', async () => {
    const { toast } = await import('sonner');
    await runLibraryTerminal('job-libcancel', {
      status: 'cancelled', total: 100, downloaded: 0, processed: 3, summarized: 0,
      blocked: [], errors: [],
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalledTimes(1);
    const warnCall = vi.mocked(toast.warning).mock.calls[0];
    if (!warnCall) throw new Error('test fixture: toast.warning was not called');
    expect(warnCall[0] as string).toMatch(/cancelled/i);
  });

  it('papers.process_library: cancelled result still names failures accrued before the stop', async () => {
    const { toast } = await import('sonner');
    await runLibraryTerminal('job-libcancelerr', {
      status: 'cancelled', total: 100, downloaded: 0, processed: 3, summarized: 0,
      blocked: [{ paper_id: 9, reason: 'no_pdf_source' }],
      errors: [
        { paper_id: 7, stage: 'process', error: 'boom' },
        { paper_id: 8, stage: 'process', error: 'boom' },
      ],
    });
    expect(toast.success).not.toHaveBeenCalled();
    const warnCall = vi.mocked(toast.warning).mock.calls[0];
    if (!warnCall) throw new Error('test fixture: toast.warning was not called');
    const msg = warnCall[0] as string;
    expect(msg).toMatch(/cancelled/i);
    expect(msg).toContain('2 failed');
    expect(msg).toContain('1 skipped');
    expect(msg).toContain('100');
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

  // ----- action_link open-redirect guard -----

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

  it('papers.scan_local: invalidates papers-feed (not the stale feed key) on success', async () => {
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":50,"progress_message":"Scanning"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-scan-local',
      kind: 'papers.scan_local',
      payload: {},
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    // Must invalidate the real feed keys used by FeedView and ResearchFeedPage
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.papers.feedAll() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.feed.counts() });
    // Must NOT invalidate the old stale key
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).not.toContain('feed');
    expect(keys).not.toContain('papers');
  });

  // ----- catch-branch reconnect uses reset delay (3d) -----

  it('subscribe: catch-branch reconnect uses reset base delay, not stale param (3d)', async () => {
    // Scenario: subscribe is called with a NON-BASE delay (simulating a prior
    // backoff step). The stream yields one successful non-terminal frame (which
    // resets currentReconnectDelay back to RECONNECT_BASE_DELAY_MS = 1000ms),
    // then throws. The catch branch must reconnect after 1000ms (reset base),
    // NOT the stale 2000ms param value.
    vi.useFakeTimers();
    const { getJob } = await import('@/lib/api');
    // getJob returns non-terminal so _reconcileOrRetry falls through to reconnect
    vi.mocked(getJob).mockResolvedValue(null as unknown as ReturnType<typeof getJob> extends Promise<infer T> ? T : never);

    const progressFrame = JSON.stringify({ status: 'running', progress: 10, progress_message: 'Going' });
    const terminalFrame = JSON.stringify({ status: 'succeeded', progress: 100 });

    // First subscription: yields one progress frame then throws
    const readerSpy = vi.spyOn(sseReader, 'createSSEReader').mockImplementationOnce(
      async function* () {
        yield progressFrame;
        throw new Error('Connection reset');
      },
    );

    // Second subscription (triggered by reconnect): yields a TERMINAL frame so the
    // stream ends with terminalReceived=true and fires NO further reconnect — this
    // keeps the call count stable at exactly 2 (an empty stream would schedule a
    // 3rd reconnect whose timing races CI).
    readerSpy.mockImplementationOnce(async function* () {
      yield terminalFrame;
    });

    const jobId = 'job-catch-delay';
    useJobStore.setState({
      jobs: { [jobId]: makeJob({ id: jobId, status: 'running' }) },
      activeAborts: {},
    });

    // Subscribe with a non-base delay (2000ms) to distinguish stale vs reset
    const STALE_DELAY_MS = 2000;
    useJobStore.getState().subscribe(jobId, STALE_DELAY_MS);

    // The reconnect chain has several await hops before the backoff sleep timer
    // is armed: the for-await rejects -> outer catch -> _reconcileOrRetry
    // (await getJob -> null) -> _reconnectAfterDrop -> sleep()->setTimeout. The
    // old version assumed a single advanceTimersByTimeAsync(0) drained ALL of
    // those microtask turns before advancing 1000ms; the variable turn count made
    // that a CI flake (3d). Instead, drain microtasks until the reconnect timer
    // actually exists, then advance — deterministic regardless of turn count.
    for (let i = 0; i < 50 && vi.getTimerCount() === 0; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }

    // The reconnect sleep is now armed. It was scheduled with the RESET base delay
    // (1000ms, set when the progress frame arrived), NOT the stale 2000ms subscribe
    // param. Advancing exactly the base delay fires it; a regression that kept the
    // stale 2000ms would NOT fire here (fake timers only run timers due within the
    // advanced window), so this assertion still catches the original bug.
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(0); // drain follow-on microtasks

    // The second createSSEReader call proves reconnect fired after 1000ms, not 2000ms.
    expect(readerSpy).toHaveBeenCalledTimes(2);
  });

  // ----- logout during backoff window (FE-DATA-01) -----

  it('subscribe: logout during backoff sleep prevents resubscribe (_reconnectAfterDrop path)', async () => {
    // Scenario: stream drops without a terminal event; _reconnectAfterDrop sleeps
    // and then checks auth. isAuthenticated is false by the time sleep resolves →
    // subscribe must NOT be called again (createSSEReader stays at 1 call).
    vi.useFakeTimers();
    const { useAuthStore } = await import('@/stores/auth-store');

    // Authenticated at subscribe time, logged out by the time the sleep resolves.
    let authenticated = true;
    vi.mocked(useAuthStore.getState).mockImplementation(() => ({
      getApiKey: vi.fn(() => (authenticated ? 'test-key' : null)),
      isAuthenticated: authenticated,
      getUser: vi.fn(() => null),
      logout: vi.fn(),
      authTime: null,
      apiKey: authenticated ? 'test-key' : null,
      user: null,
      lastError: null,
      login: vi.fn(),
      loginWithSession: vi.fn(),
      isSessionValid: vi.fn(() => authenticated),
      expireSession: vi.fn(),
      hydrateFromCookie: vi.fn(),
    }));

    const readerSpy = vi.spyOn(sseReader, 'createSSEReader').mockImplementationOnce(
      // Stream closes immediately without a terminal event → triggers _reconcileOrRetry
      // → getJob returns null → _reconnectAfterDrop → sleep → guard check
      async function* () {
        // Yield nothing; stream ends with terminalReceived=false
      },
    );

    const { getJob } = await import('@/lib/api');
    vi.mocked(getJob).mockResolvedValue(null as unknown as ReturnType<typeof getJob> extends Promise<infer T> ? T : never);

    const jobId = 'job-logout-backoff';
    useJobStore.setState({
      jobs: { [jobId]: makeJob({ id: jobId, status: 'running' }) },
      activeAborts: {},
    });

    useJobStore.getState().subscribe(jobId);

    // Drain microtasks until the reconnect sleep timer is armed
    for (let i = 0; i < 50 && vi.getTimerCount() === 0; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    // Simulate logout during the backoff sleep
    authenticated = false;

    // Fire the sleep timer — the guard should check isAuthenticated and bail
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(0);

    // createSSEReader was called exactly once (the original subscribe); no resubscribe
    expect(readerSpy).toHaveBeenCalledTimes(1);
  });

  it('subscribe: logout during streaming_timeout backoff sleep prevents resubscribe', async () => {
    // Scenario: a streaming_timeout sentinel arrives; the store sleeps before
    // resubscribing. isAuthenticated becomes false during the sleep → no resubscribe.
    vi.useFakeTimers();
    const { useAuthStore } = await import('@/stores/auth-store');

    let authenticated = true;
    vi.mocked(useAuthStore.getState).mockImplementation(() => ({
      getApiKey: vi.fn(() => (authenticated ? 'test-key' : null)),
      isAuthenticated: authenticated,
      getUser: vi.fn(() => null),
      logout: vi.fn(),
      authTime: null,
      apiKey: authenticated ? 'test-key' : null,
      user: null,
      lastError: null,
      login: vi.fn(),
      loginWithSession: vi.fn(),
      isSessionValid: vi.fn(() => authenticated),
      expireSession: vi.fn(),
      hydrateFromCookie: vi.fn(),
    }));

    const readerSpy = vi.spyOn(sseReader, 'createSSEReader').mockImplementationOnce(
      async function* () {
        yield JSON.stringify({ status: 'streaming_timeout' });
      },
    );

    const jobId = 'job-timeout-backoff';
    useJobStore.setState({
      jobs: { [jobId]: makeJob({ id: jobId, status: 'running' }) },
      activeAborts: {},
    });

    useJobStore.getState().subscribe(jobId);

    // Drain microtasks until the backoff sleep timer is armed
    for (let i = 0; i < 50 && vi.getTimerCount() === 0; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    // Simulate logout during the sleep
    authenticated = false;

    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(0);

    // Only the initial subscribe call; no resubscribe after the logout guard fires
    expect(readerSpy).toHaveBeenCalledTimes(1);
  });

  // ----- createSSEReader integration -----

  it('subscribe: uses createSSEReader to stream job events', async () => {
    // Spy on createSSEReader to confirm subscribe delegates to it.
    const readerSpy = vi.spyOn(sseReader, 'createSSEReader');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":10,"progress_message":"Going"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done"}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-sse-spy',
      kind: 'pulse.generate',
      payload: {},
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    // createSSEReader must have been called with the job stream URL
    expect(readerSpy).toHaveBeenCalledWith(
      '/api/jobs/job-sse-spy/stream',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  // ----- eviction-timer + logout-scoped backoff hygiene -----
  //
  // NOTE: these tests deliberately avoid asserting on vi.getTimerCount() —
  // unfinished reconnect chains from earlier tests in this file ("zombies")
  // schedule timers of their own at unpredictable await points. All assertions
  // are therefore scoped to this test's job id (removeJob spy args / stream URL
  // filter), which zombies can never touch.

  /**
   * Swap the store's removeJob for a spy so a firing eviction timer is
   * observable (the timer callback resolves removeJob via get() at fire time).
   * Returns the spy + a restore function. The spy survives _reset because
   * JOB_INITIAL_STATE only carries jobs/activeAborts.
   */
  function spyOnRemoveJob(): { removeSpy: ReturnType<typeof vi.fn<(jobId: string) => void>>; restore: () => void } {
    const origRemove = useJobStore.getState().removeJob;
    const removeSpy = vi.fn<(jobId: string) => void>();
    useJobStore.setState({ removeJob: removeSpy });
    return {
      removeSpy,
      restore: () => useJobStore.setState({ removeJob: origRemove }),
    };
  }

  it('eviction timer fires removeJob after the eviction delay when not cancelled (control)', async () => {
    // Positive control: proves cancelJob really arms an eviction timer, so the
    // cancellation tests below are not vacuously green.
    vi.useFakeTimers();
    const { cancelJob: apiCancelJob } = await import('@/lib/api');
    vi.mocked(apiCancelJob).mockResolvedValue(undefined);

    useJobStore.setState({
      jobs: { 'job-evict-fires': makeJob({ id: 'job-evict-fires', status: 'running' }) },
      activeAborts: {},
    });
    await useJobStore.getState().cancelJob('job-evict-fires');

    const { removeSpy, restore } = spyOnRemoveJob();
    try {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000 + 1); // past EVICT_DELAY_MS
      expect(removeSpy).toHaveBeenCalledWith('job-evict-fires');
    } finally {
      restore();
    }
  });

  it('_reset: cancels both terminal-eviction and cancel-eviction timers', async () => {
    vi.useFakeTimers();
    const { cancelJob: apiCancelJob } = await import('@/lib/api');
    vi.mocked(apiCancelJob).mockResolvedValue(undefined);

    // Job A reaches a terminal state via SSE → post-terminal eviction timer.
    vi.spyOn(sseReader, 'createSSEReader').mockImplementation(async function* (url) {
      if (url === '/api/jobs/job-evict-terminal/stream') {
        yield JSON.stringify({ status: 'succeeded', progress: 100 });
      }
      // any other URL (zombie reconnects): end immediately
    });
    useJobStore.setState({
      jobs: {
        'job-evict-terminal': makeJob({ id: 'job-evict-terminal', status: 'running' }),
        'job-evict-cancel': makeJob({ id: 'job-evict-cancel', status: 'running' }),
      },
      activeAborts: {},
    });
    useJobStore.getState().subscribe('job-evict-terminal');

    // Drain microtasks until the terminal event is processed (eviction armed).
    for (
      let i = 0;
      i < 50 && useJobStore.getState().jobs['job-evict-terminal']?.status !== 'succeeded';
      i++
    ) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(requireJob(useJobStore.getState().jobs['job-evict-terminal']).status).toBe('succeeded');

    // Job B is cancelled → post-cancel eviction timer.
    await useJobStore.getState().cancelJob('job-evict-cancel');
    expect(requireJob(useJobStore.getState().jobs['job-evict-cancel']).status).toBe('cancelled');

    const { removeSpy, restore } = spyOnRemoveJob();
    try {
      useJobStore.getState()._reset(); // logout

      // Neither eviction timer may fire into post-logout state.
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000 + 1000); // past EVICT_DELAY_MS
      expect(removeSpy).not.toHaveBeenCalledWith('job-evict-terminal');
      expect(removeSpy).not.toHaveBeenCalledWith('job-evict-cancel');
    } finally {
      restore();
    }
  });

  it('removeJob: clears a pending eviction timer for an early-dismissed job', async () => {
    vi.useFakeTimers();
    const { cancelJob: apiCancelJob } = await import('@/lib/api');
    vi.mocked(apiCancelJob).mockResolvedValue(undefined);

    useJobStore.setState({
      jobs: { 'job-dismiss': makeJob({ id: 'job-dismiss', status: 'running' }) },
      activeAborts: {},
    });
    await useJobStore.getState().cancelJob('job-dismiss'); // eviction armed (see control test)

    // User dismisses the cancelled job before the eviction delay elapses.
    useJobStore.getState().removeJob('job-dismiss');

    const { removeSpy, restore } = spyOnRemoveJob();
    try {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000 + 1000);
      // The orphaned timer must NOT fire again for the already-removed job.
      expect(removeSpy).not.toHaveBeenCalledWith('job-dismiss');
    } finally {
      restore();
    }
  });

  it('_reset during reconnect backoff aborts the pending sleep via the logout-scoped signal', async () => {
    // Auth stays TRUE throughout — proves the resubscribe is stopped by the
    // logout-scoped signal aborting the sleep, NOT by the post-sleep
    // isAuthenticated guard.
    vi.useFakeTimers();
    const { useAuthStore } = await import('@/stores/auth-store');
    const { getJob } = await import('@/lib/api');
    vi.mocked(useAuthStore.getState).mockReturnValue({
      getApiKey: vi.fn(() => 'test-key'),
      getUser: vi.fn(() => null),
      logout: vi.fn(),
      isAuthenticated: true,
      authTime: null,
      apiKey: 'test-key',
      user: null,
      lastError: null,
      login: vi.fn(),
      loginWithSession: vi.fn(),
      isSessionValid: vi.fn(() => true),
      expireSession: vi.fn(),
      hydrateFromCookie: vi.fn(),
    });
    vi.mocked(getJob).mockResolvedValue(null as unknown as ReturnType<typeof getJob> extends Promise<infer T> ? T : never);

    // Streams end without a terminal event → _reconcileOrRetry → getJob null →
    // _reconnectAfterDrop → backoff sleep (for our job AND for zombies).
    const readerSpy = vi.spyOn(sseReader, 'createSSEReader').mockImplementation(
      async function* () {
        // Yield nothing
      },
    );

    const jobId = 'job-reset-backoff';
    const streamUrl = `/api/jobs/${jobId}/stream`;
    const ourCalls = () => readerSpy.mock.calls.filter((c) => c[0] === streamUrl).length;

    useJobStore.setState({
      jobs: { [jobId]: makeJob({ id: jobId, status: 'running' }) },
      activeAborts: {},
    });
    useJobStore.getState().subscribe(jobId);

    // Drain microtasks until OUR job's reconcile poll happened; everything from
    // there to the backoff sleep is pure microtasks, so a bounded extra drain
    // deterministically arms the sleep.
    for (
      let i = 0;
      i < 50 && !vi.mocked(getJob).mock.calls.some((c) => c[0] === jobId);
      i++
    ) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(getJob).toHaveBeenCalledWith(jobId);
    for (let i = 0; i < 20; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(ourCalls()).toBe(1); // backoff pending, not yet resubscribed

    useJobStore.getState()._reset(); // logout

    // Advancing far past every backoff step must produce NO resubscribe for
    // our job: the logout-scoped signal rejected the pending sleep.
    await vi.advanceTimersByTimeAsync(10_000);
    for (let i = 0; i < 20; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(ourCalls()).toBe(1);
  });

  it('subscribe: streaming_timeout backoff completes and resubscribes when not logged out (G-01 guard)', async () => {
    // The backoff sleep must receive the logout-scoped signal, NOT the SSE
    // controller's (already-aborted) signal. A G-01 regression would reject the
    // sleep immediately → no resubscribe for this job.
    vi.useFakeTimers();
    const jobId = 'job-timeout-normal';
    const streamUrl = `/api/jobs/${jobId}/stream`;

    let ourCall = 0;
    const readerSpy = vi.spyOn(sseReader, 'createSSEReader').mockImplementation(
      async function* (url) {
        if (url !== streamUrl) return; // zombie reconnects: end immediately
        ourCall += 1;
        yield ourCall === 1
          ? JSON.stringify({ status: 'streaming_timeout' })
          : JSON.stringify({ status: 'succeeded', progress: 100 });
      },
    );
    const ourCalls = () => readerSpy.mock.calls.filter((c) => c[0] === streamUrl).length;

    useJobStore.setState({
      jobs: { [jobId]: makeJob({ id: jobId, status: 'running' }) },
      activeAborts: {},
    });
    useJobStore.getState().subscribe(jobId);

    // The path from subscribe to the backoff sleep is pure microtasks — drain.
    for (let i = 0; i < 20; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(ourCalls()).toBe(1); // backoff pending — no instant resubscribe

    // Sleep completes after the base delay → store resubscribes (auth stays true).
    await vi.advanceTimersByTimeAsync(1000);
    for (let i = 0; i < 20; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(ourCalls()).toBe(2);
  });

  it('_reset re-arms the logout signal: backoff sleeps after a later login are not pre-aborted', async () => {
    // A logout aborts the logout-scoped controller; _reset must replace it so
    // the NEXT session's reconnect backoffs still work. A regression that kept
    // the aborted controller would reject every future sleep instantly.
    vi.useFakeTimers();
    useJobStore.getState()._reset(); // simulate a previous logout

    const jobId = 'job-relogin-backoff';
    const streamUrl = `/api/jobs/${jobId}/stream`;

    let ourCall = 0;
    const readerSpy = vi.spyOn(sseReader, 'createSSEReader').mockImplementation(
      async function* (url) {
        if (url !== streamUrl) return; // zombie reconnects: end immediately
        ourCall += 1;
        yield ourCall === 1
          ? JSON.stringify({ status: 'streaming_timeout' })
          : JSON.stringify({ status: 'succeeded', progress: 100 });
      },
    );
    const ourCalls = () => readerSpy.mock.calls.filter((c) => c[0] === streamUrl).length;

    useJobStore.setState({
      jobs: { [jobId]: makeJob({ id: jobId, status: 'running' }) },
      activeAborts: {},
    });
    useJobStore.getState().subscribe(jobId);

    for (let i = 0; i < 20; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(ourCalls()).toBe(1); // sleep armed with the FRESH signal — no instant abort

    await vi.advanceTimersByTimeAsync(1000);
    for (let i = 0; i < 20; i++) {
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(ourCalls()).toBe(2); // backoff ran to completion; resubscribed
  });

  // ----- paper.summarize coverage/passes -> paper-detail cache -----

  function seedPaperDetail(paperId: number) {
    queryClient.setQueryData(QUERY_KEYS.papers.detail(paperId), {
      paper: { id: paperId, title: 'P' },
      summary: { id: 1, paper_id: paperId, summary_brief: 'b', summary_detailed: 'd' },
      chunks: [],
      user_state: null,
    });
  }

  it('paper.summarize: merges coverage 0 from the job result into the cached summary', async () => {
    const paperId = 88;
    seedPaperDetail(paperId);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"running","progress":50,"progress_message":"Summarizing"}\n\n',
          'data: {"status":"succeeded","progress":100,"progress_message":"Done","result":{"paper_id":88,"coverage":0,"passes":1}}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-sum-cov',
      kind: 'paper.summarize',
      payload: { paper_id: paperId },
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    const cached = queryClient.getQueryData(QUERY_KEYS.papers.detail(paperId)) as {
      summary: { coverage?: number; summary_brief: string };
    };
    expect(cached.summary.coverage).toBe(0); // banner state merged from job result
    expect(cached.summary.summary_brief).toBe('b'); // merge, not replace
  });

  it('paper.summarize: a clean job (no coverage/passes) leaves the cached summary untouched', async () => {
    const paperId = 89;
    seedPaperDetail(paperId);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        createMockSSEStream([
          'data: {"status":"succeeded","progress":100,"progress_message":"Done","result":{"paper_id":89,"summary_id":5,"status":"summarized"}}\n\n',
          'data: [DONE]\n\n',
        ]),
        { status: 200 },
      ),
    );

    useJobStore.getState().trackExternalJob({
      jobId: 'job-sum-clean',
      kind: 'paper.summarize',
      payload: { paper_id: paperId },
      status: 'queued',
    });

    await new Promise((r) => setTimeout(r, 50));

    const cached = queryClient.getQueryData(QUERY_KEYS.papers.detail(paperId)) as {
      summary: { coverage?: number };
    };
    expect(cached.summary.coverage).toBeUndefined(); // no keys -> cache untouched
  });
});

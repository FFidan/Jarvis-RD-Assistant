/**
 * Zustand job store — tracks all background jobs (Pulse, PDF processing, etc.)
 * and manages SSE subscriptions for live progress updates.
 *
 * Persisted to sessionStorage (jobs are short-lived and should not outlast
 * the browser tab). AbortControllers are NOT persisted — they are recreated
 * on hydration by re-subscribing to any running jobs.
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import { toast } from 'sonner';
import { useAuthStore } from '@/stores/auth-store';
import { registerSessionReset } from '@/stores/session-reset';
import { createJob as apiCreateJob, listJobs as apiListJobs, cancelJob as apiCancelJob, getJob as apiGetJob } from '@/lib/api';
import { createSSEReader, SSEGetError } from '@/lib/sse-reader';
import { handleAuthFailure } from '@/lib/api/core';
import { queryClient } from '@/lib/query-client';
import { getNavigate } from '@/lib/navigate-bridge';
import { isSafeRelativeHref } from '@/lib/safe-href';
import { QUERY_KEYS } from '@/lib/query-keys';
import { kindLabel } from '@/lib/labels/jobKinds';
import { errorMessage } from '@/lib/errors';
import type { PaperDetail } from '@/types';

/**
 * Per-kind query invalidation: when a job of the given kind reaches
 * `succeeded`, each listed query key is invalidated so the UI refetches
 * the new state (e.g. the freshly generated Pulse deck).
 *
 * Values are functions so paper_id etc. can be threaded through from payload.
 */
const INVALIDATE_ON_SUCCESS: Record<string, (job: Job) => readonly (readonly unknown[])[]> = {
  'pulse.generate':         () => [QUERY_KEYS.pulse.today(), QUERY_KEYS.pulse.statsAll()],
  'paper.process':          (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null
      ? [QUERY_KEYS.actionItems.unprocessed()]
      : [QUERY_KEYS.papers.detail(paperId), QUERY_KEYS.actionItems.unprocessed()];
  },
  'paper.summarize':        (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null ? [] : [QUERY_KEYS.papers.detail(paperId)];
  },
  'card.generate':          () => [QUERY_KEYS.decks.list(), QUERY_KEYS.cards.all()],
  'paper.analyze':          (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null ? [] : [QUERY_KEYS.papers.detail(paperId)];
  },
  'papers.batch_process':   () => [QUERY_KEYS.papers.feedAll(), QUERY_KEYS.feed.counts(), QUERY_KEYS.actionItems.unprocessed()],
  'papers.process_library': () => [QUERY_KEYS.papers.feedAll(), QUERY_KEYS.feed.counts(), QUERY_KEYS.actionItems.unprocessed()],
  'papers.scan_local':      () => [QUERY_KEYS.papers.feedAll(), QUERY_KEYS.feed.counts()],
  'papers.batch_summarize': () => [QUERY_KEYS.papers.feedAll()],
  'extraction.single':      (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null ? [['extraction-table']] : [QUERY_KEYS.papers.detail(paperId), ['extraction-table']]; // Note: ['extraction-table'] bare prefix — no registry factory for all entries
  },
  'extraction.batch':       () => [['extraction-table']], // Note: bare prefix for invalidation — no registry factory for all extraction-table entries
  'digest.weekly':          () => [QUERY_KEYS.digest.weekly()],
  'contradictions.scan':    (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null
      ? [['contradictions'], QUERY_KEYS.consensus.all()] // Note: bare 'contradictions' prefix — no registry factory for all entries
      : [QUERY_KEYS.contradictions.verified(paperId), QUERY_KEYS.papers.detail(paperId)];
  },
  'zotero.push':            (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null ? [] : [QUERY_KEYS.zotero.linkage(paperId), QUERY_KEYS.papers.detail(paperId)];
  },
  'zotero.resync':          (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null ? [] : [QUERY_KEYS.zotero.linkage(paperId), QUERY_KEYS.papers.detail(paperId)];
  },
  'zotero.sync_annotations': (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null ? [] : [QUERY_KEYS.notes.user(paperId), QUERY_KEYS.notes.zotero(paperId)];
  },
  'zotero.push_highlights': (j) => {
    const paperId = getPaperIdFromJob(j);
    return paperId == null ? [] : [QUERY_KEYS.highlights.list(paperId)];
  },
  'zotero.poll':            () => [QUERY_KEYS.papers.feedAll(), QUERY_KEYS.feed.counts()],
  'zotero.sync_from_zotero': () => [QUERY_KEYS.papers.feedAll(), QUERY_KEYS.feed.counts()],
};

/**
 * A `zotero.push_highlights` job reaches `succeeded` even when the export only
 * partially worked — the handler returns a result `status` rather than raising —
 * so a green "completed" toast would misreport a failure. Any non-`ok` status
 * gets a warning that names the reason instead.
 */
const ZOTERO_PUSH_FAILURE_MESSAGES: Record<string, string> = {
  not_linked: 'Link this paper to Zotero before exporting highlights.',
  pdf_unavailable: 'The PDF is unavailable, so highlights could not be exported.',
  quota_exceeded: 'Your Zotero storage is full — free up space and try again.',
  config_decrypt_failed: 'Re-save your Zotero API key in Settings, then try again.',
  disabled: 'Connect Zotero in Settings to export highlights.',
};
const zoteroPushWarning = (status: string): string =>
  ZOTERO_PUSH_FAILURE_MESSAGES[status] ?? 'Some highlights could not be exported to Zotero.';

/** Failed, skipped and total paper counts from a `papers.process_library` result. */
const libraryCounts = (result: Job['result']) => {
  const r = (result ?? {}) as { total?: number; errors?: unknown[]; blocked?: unknown[] };
  const failed = Array.isArray(r.errors) ? r.errors.length : 0;
  const skipped = Array.isArray(r.blocked) ? r.blocked.length : 0;
  const total = typeof r.total === 'number' ? r.total : failed + skipped;
  return { failed, skipped, total };
};

/** The count fragments a library warning names, omitting the zero ones. */
const libraryOutcomeParts = (failed: number, skipped: number): string[] => {
  const parts: string[] = [];
  if (failed > 0) parts.push(`${failed} failed`);
  if (skipped > 0) parts.push(`${skipped} skipped (no PDF source)`);
  return parts;
};

/**
 * A `papers.process_library` job reaches `succeeded` even when some papers
 * failed or were skipped (no PDF source) — the handler returns a `status`
 * rather than raising. A green "completed" toast would misreport that, so a
 * `partial` result gets a warning naming both counts.
 */
const libraryPartialWarning = (result: Job['result']): string => {
  const { failed, skipped, total } = libraryCounts(result);
  const parts = libraryOutcomeParts(failed, skipped);
  const detail = parts.length > 0 ? parts.join(', ') : 'no work completed';
  return `Library processing finished - ${detail} of ${total}; open Jobs for details`;
};

/**
 * A cancelled run also reaches `succeeded` — it stopped early rather than
 * failing — so the warning names both the papers it never reached and whatever
 * had already failed or been skipped before the stop.
 */
const libraryCancelledWarning = (result: Job['result']): string => {
  const { failed, skipped, total } = libraryCounts(result);
  const parts = [
    `stopped before finishing ${total} papers`,
    ...libraryOutcomeParts(failed, skipped),
  ];
  return `Library processing was cancelled - ${parts.join(', ')}; open Jobs for details`;
};

/** Terminal statuses — job will not receive more events. */
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);

/** Backoff constants for SSE reconnect attempts (ms). */
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 8000;

/** Resolves after `ms` milliseconds, or rejects early if the signal fires. */
const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('Aborted', 'AbortError')); return; }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
  });

/** Delay before evicting terminal jobs from the store (ms). */
const EVICT_DELAY_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Pending eviction timers keyed by job id. Handles are tracked so an
 * early removeJob — or a logout `_reset` — cancels them instead of leaving
 * orphaned timers firing against cleared state.
 */
const evictionTimers = new Map<string, ReturnType<typeof setTimeout>>();

function cancelEviction(jobId: string): void {
  const handle = evictionTimers.get(jobId);
  if (handle !== undefined) {
    clearTimeout(handle);
    evictionTimers.delete(jobId);
  }
}

function cancelAllEvictions(): void {
  for (const handle of evictionTimers.values()) clearTimeout(handle);
  evictionTimers.clear();
}

function scheduleEviction(jobId: string, evict: () => void): void {
  cancelEviction(jobId); // never leave two live timers for one job
  evictionTimers.set(jobId, setTimeout(evict, EVICT_DELAY_MS));
}

/**
 * Logout-scoped abort controller: cancels reconnect-backoff sleeps on `_reset`
 * (logout). Deliberately SEPARATE from each subscription's SSE AbortController —
 * by the time a backoff sleep starts, `_cleanupSubscription` has already aborted
 * the SSE controller, so passing the SSE signal to sleep() would throw
 * AbortError immediately (the G-01 trap). Re-armed after every reset so a
 * subsequent login starts with a fresh, un-aborted signal.
 */
let logoutAbort = new AbortController();

function getPaperIdFromJob(job: Job): number | null {
  const paperId = job.payload?.paper_id;
  return typeof paperId === 'number' && Number.isFinite(paperId) ? paperId : null;
}

/**
 * Merge a finished `paper.summarize` job's `coverage`/`passes` into the cached
 * paper-detail summary so the research-log banners render from the real flow.
 *
 * The backend puts these on the JOB result (omit-when-clean), but the persisted
 * GET /api/papers/:id summary never carries them (non-persisted, always null),
 * so the invalidation refetch alone would wipe any banner. We patch the cache
 * directly here. Merge (not replace): absent keys leave the cached value
 * untouched, and a clean job (no coverage/passes) leaves the cache alone.
 */
function applySummaryCoverageToCache(job: Job): void {
  const paperId = getPaperIdFromJob(job);
  if (paperId == null) return;
  const result = job.result as { coverage?: number; passes?: number } | null;
  if (result == null) return;
  const hasCoverage = typeof result.coverage === 'number';
  const hasPasses = typeof result.passes === 'number';
  if (!hasCoverage && !hasPasses) return; // clean job — nothing to patch

  queryClient.setQueryData<PaperDetail>(QUERY_KEYS.papers.detail(paperId), (prev) => {
    if (!prev || !prev.summary) return prev; // no cached detail/summary to merge into
    return {
      ...prev,
      summary: {
        ...prev.summary,
        ...(hasCoverage ? { coverage: result.coverage } : {}),
        ...(hasPasses ? { passes: result.passes } : {}),
      },
    };
  });
}

export interface Job {
  id: string;
  kind: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  progress: number;
  progress_message: string | null;
  payload?: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error: {
    message: string;
    action_link?: { label: string; href: string };
  } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

const JOB_INITIAL_STATE = {
  jobs: {} as Record<string, Job>,
  activeAborts: {} as Record<string, AbortController>,
};

interface JobStore {
  jobs: Record<string, Job>;
  /** AbortControllers for active SSE subscriptions — NOT persisted. */
  activeAborts: Record<string, AbortController>;

  /** POST a new job + subscribe to its SSE stream. Returns the job_id. */
  startJob: (kind: string, payload: unknown) => Promise<string>;
  /** Register an externally created job id and subscribe if it is active. */
  trackExternalJob: (job: { jobId: string; kind: string; payload: Record<string, unknown>; status?: Job['status'] }) => string;
  /** Hook up SSE stream for an existing job id. */
  subscribe: (jobId: string, reconnectDelay?: number) => void;
  /** Cancel a running job. */
  cancelJob: (jobId: string) => Promise<void>;
  /** Remove a job from the store immediately (e.g. user dismisses). */
  removeJob: (jobId: string) => void;
  /** Returns true when a job of this kind is queued or running. */
  hasRunning: (kind: string) => boolean;
  /**
   * Returns true when a job of the given kind is queued or running AND every
   * key/value pair in `payload` matches the job's payload.
   */
  isRunning: (kind: string, payload: Record<string, unknown>) => boolean;
  /** On app mount: re-subscribe to any jobs that are still running. */
  hydrate: () => Promise<void>;
  /** Reset to initial state (called on logout to prevent cross-user leakage). */
  _reset: () => void;

  // Internal helpers
  _upsertJob: (job: Job) => void;
  _cleanupSubscription: (jobId: string) => void;
}

export const useJobStore = create<JobStore>()(
  persist(
    (set, get) => ({
      ...JOB_INITIAL_STATE,

      _upsertJob(job: Job) {
        set((state) => ({
          jobs: { ...state.jobs, [job.id]: job },
        }));
      },

      _cleanupSubscription(jobId: string) {
        const ctrl = get().activeAborts[jobId];
        if (ctrl) {
          ctrl.abort();
          set((state) => {
            const { [jobId]: _removed, ...rest } = state.activeAborts;
            return { activeAborts: rest };
          });
        }
      },

      async startJob(kind, payload) {
        const { job_id } = await apiCreateJob(kind, payload);
        get().trackExternalJob({
          jobId: job_id,
          kind,
          payload: payload as Record<string, unknown>,
          status: 'queued',
        });
        return job_id;
      },

      trackExternalJob({ jobId, kind, payload, status = 'queued' }) {
        const placeholder: Job = {
          id: jobId,
          kind,
          status,
          progress: 0,
          progress_message: null,
          payload,
          result: null,
          error: null,
          created_at: new Date().toISOString(),
          started_at: null,
          finished_at: null,
        };
        get()._upsertJob(placeholder);
        if (status === 'queued' || status === 'running') {
          get().subscribe(jobId);
        }
        return jobId;
      },

      subscribe(jobId, reconnectDelay = RECONNECT_BASE_DELAY_MS) {
        // Avoid double-subscribing
        if (get().activeAborts[jobId]) return;

        const controller = new AbortController();
        set((state) => ({
          activeAborts: { ...state.activeAborts, [jobId]: controller },
        }));

        const apiKey = useAuthStore.getState().getApiKey();
        const headers: Record<string, string> = apiKey ? { 'X-API-Key': apiKey } : {};

        // Internal helper: fire toast + invalidate queries for a terminal job
        const _handleTerminal = (job: Job) => {
          if (job.status === 'succeeded') {
            // Zero-card generation is a degraded outcome — the paper view shows
            // its own warning; a green "completed" toast would contradict it.
            const zeroCards =
              job.kind === 'card.generate' &&
              (job.result as { cards_created?: number } | null)?.cards_created === 0;
            // A highlight export can succeed-with-failures; warn instead of a
            // green toast when the export status is anything but "ok".
            const zoteroPushStatus =
              job.kind === 'zotero.push_highlights'
                ? (job.result as { status?: string } | null)?.status
                : undefined;
            // A whole-library run can succeed-with-failures or stop on cancel;
            // `partial` and `cancelled` warn instead of a green success toast.
            const libraryStatus =
              job.kind === 'papers.process_library'
                ? (job.result as { status?: string } | null)?.status
                : undefined;
            if (zoteroPushStatus && zoteroPushStatus !== 'ok') {
              toast.warning(zoteroPushWarning(zoteroPushStatus));
            } else if (libraryStatus === 'cancelled') {
              toast.warning(libraryCancelledWarning(job.result));
            } else if (libraryStatus === 'partial') {
              toast.warning(libraryPartialWarning(job.result));
            } else if (!zeroCards) {
              toast.success(`${kindLabel(job.kind)} completed`);
            }
            // Patch coverage/passes into the paper-detail cache BEFORE invalidating:
            // the refetched GET never carries them, so the banner state must be
            // merged from the job result here.
            if (job.kind === 'paper.summarize') applySummaryCoverageToCache(job);
            const keysFactory = INVALIDATE_ON_SUCCESS[job.kind];
            if (keysFactory) {
              for (const key of keysFactory(job)) {
                queryClient.invalidateQueries({ queryKey: key });
              }
            }
          } else if (job.status === 'failed') {
            const msg = job.error?.message ?? `${kindLabel(job.kind)} failed`;
            const actionLink = job.error?.action_link;
            if (actionLink) {
              toast.error(msg, {
                action: {
                  label: actionLink.label,
                  onClick: () => {
                    if (isSafeRelativeHref(actionLink.href)) {
                      const nav = getNavigate();
                      if (nav) {
                        nav(actionLink.href);
                      } else {
                        window.location.href = actionLink.href;
                      }
                    } else {
                      console.warn('Refusing non-relative action_link:', actionLink.href);
                    }
                  },
                },
              });
            } else {
              toast.error(msg);
            }
          }
          get()._cleanupSubscription(jobId);
          scheduleEviction(jobId, () => get().removeJob(jobId));
        };

        const _reconnectAfterDrop = async (delayMs: number) => {
          get()._cleanupSubscription(jobId);
          // G-01: do NOT pass this subscription's SSE signal to sleep — the
          // _cleanupSubscription above just aborted it, so it would throw
          // AbortError immediately. The logout-scoped signal only aborts on
          // _reset, cancelling the pending backoff when the user logs out.
          try {
            await sleep(delayMs, logoutAbort.signal);
          } catch {
            return; // logged out during the backoff — drop the reconnect
          }
          if (!useAuthStore.getState().isAuthenticated) return;
          const nextDelay = Math.min(delayMs * 2, RECONNECT_MAX_DELAY_MS);
          get().subscribe(jobId, nextDelay);
        };

        const _reconcileOrRetry = async (delayMs: number) => {
          try {
            const finalJob = await apiGetJob(jobId).catch(() => null);
            if (finalJob) {
              set((s) => ({ jobs: { ...s.jobs, [jobId]: finalJob } }));
              if (TERMINAL_STATUSES.has(finalJob.status)) {
                _handleTerminal(finalJob);
                return;
              }
            }
          } catch {
            /* best-effort: fall through to reconnect */
          }

          await _reconnectAfterDrop(delayMs);
        };

        // Stream job events via GET SSE endpoint using createSSEReader.
        // createSSEReader handles buffering, line-splitting, [DONE] sentinel, and
        // reader.cancel() in its finally block — eliminating the inline reader loop.
        (async () => {
          let terminalReceived = false;
          // Track current backoff delay; reset to base on every successful frame
          let currentReconnectDelay = reconnectDelay;
          try {

            for await (const raw of createSSEReader(`/api/jobs/${jobId}/stream`, {
              headers,
              signal: controller.signal,
            })) {
              try {
                const event = JSON.parse(raw) as Partial<Job>;
                // streaming_timeout sentinel — treat as non-terminal; reconnect with backoff
                if ((event as { status?: string }).status === 'streaming_timeout') {
                  get()._cleanupSubscription(jobId);
                  // controller is already aborted by _cleanupSubscription, so do NOT
                  // pass its signal to sleep — it would throw AbortError immediately (G-01).
                  // The logout-scoped signal is safe here: it only fires on _reset.
                  // Explicit try/catch so the surrounding malformed-frame catch
                  // cannot swallow the abort and keep iterating a dead stream.
                  try {
                    await sleep(currentReconnectDelay, logoutAbort.signal);
                  } catch {
                    return; // logged out during the backoff — stop reconnecting
                  }
                  if (!useAuthStore.getState().isAuthenticated) return;
                  const nextDelay = Math.min(currentReconnectDelay * 2, RECONNECT_MAX_DELAY_MS);
                  get().subscribe(jobId, nextDelay);
                  return;
                }
                const current = get().jobs[jobId] ?? {};
                const updated: Job = {
                  ...(current as Job),
                  ...event,
                  id: jobId,
                };
                get()._upsertJob(updated);
                // Successful event frame received — reset backoff for any future reconnect
                currentReconnectDelay = RECONNECT_BASE_DELAY_MS;

                if (TERMINAL_STATUSES.has(updated.status)) {
                  terminalReceived = true;
                  _handleTerminal(updated);
                  break;
                }
              } catch {
                /* skip malformed frames */
              }
            }

            // Stream ended (either [DONE], connection drop, or terminal break).
            // If no terminal job status was received, reconcile via REST poll.
            if (!terminalReceived) {
              await _reconcileOrRetry(currentReconnectDelay);
            }
          } catch (err) {
            // AbortError means we intentionally cancelled — not an error.
            if (err instanceof DOMException && err.name === 'AbortError') return;
            // SSE GET auth failure: 401 ends the session (debounced logout via handleAuthFailure);
            // 403 is a permission error — drop the job but do NOT log out.
            if (err instanceof SSEGetError && (err.status === 401 || err.status === 403)) {
              get().removeJob(jobId);
              handleAuthFailure(err.status);
              return;
            }
            // Other errors: reconcile if possible, otherwise reconnect with backoff.
            await _reconcileOrRetry(currentReconnectDelay);
          }
        })();
      },

      async cancelJob(jobId) {
        try {
          await apiCancelJob(jobId);
        } catch (err) {
          const msg = errorMessage(err, 'Failed to cancel job');
          toast.error(msg);
          get().subscribe(jobId);
          return;
        }
        get()._cleanupSubscription(jobId);
        // Optimistically update local status
        const job = get().jobs[jobId];
        if (job) {
          get()._upsertJob({ ...job, status: 'cancelled' });
          // Schedule eviction (handle tracked so removeJob/_reset can cancel it)
          scheduleEviction(jobId, () => get().removeJob(jobId));
        }
      },

      removeJob(jobId) {
        cancelEviction(jobId);
        get()._cleanupSubscription(jobId);
        set((state) => {
          const { [jobId]: _removed, ...rest } = state.jobs;
          return { jobs: rest };
        });
      },

      hasRunning(kind) {
        return Object.values(get().jobs).some(
          (j) => j.kind === kind && (j.status === 'running' || j.status === 'queued'),
        );
      },

      isRunning(kind, payload) {
        return Object.values(get().jobs).some((j) => {
          if (j.kind !== kind) return false;
          if (j.status !== 'running' && j.status !== 'queued') return false;
          return Object.entries(payload).every(
            ([k, v]) => j.payload != null && j.payload[k] === v,
          );
        });
      },

      async hydrate() {
        try {
          const [running, queued] = await Promise.all([
            apiListJobs({ status: 'running' }),
            apiListJobs({ status: 'queued' }),
          ]);
          const jobs = [...running, ...queued];
          for (const job of jobs) {
            get()._upsertJob(job);
            // Only subscribe if not already subscribed
            if (!get().activeAborts[job.id]) {
              get().subscribe(job.id);
            }
          }
        } catch {
          /* best-effort: if server is down, don't crash the app */
        }
      },

      _reset() {
        // Abort all active subscriptions before wiping state.
        for (const ctrl of Object.values(get().activeAborts)) {
          ctrl.abort();
        }
        // Cancel any reconnect backoff currently sleeping, then re-arm the
        // logout signal so the next login starts un-aborted.
        logoutAbort.abort();
        logoutAbort = new AbortController();
        // Cancel all pending eviction timers so none fire post-logout.
        cancelAllEvictions();
        set(JOB_INITIAL_STATE);
      },
    }),
    {
      name: 'jarvis-jobs',
      storage: createJSONStorage(() => sessionStorage),
      // Only persist the jobs map — AbortControllers cannot be serialised
      partialize: (state) => ({
        jobs: state.jobs,
      }),
    },
  ),
);

/**
 * Register a document `visibilitychange` listener that re-hydrates job state
 * whenever the user returns to the tab (e.g. after the screen has been locked
 * or the user switched away for a long time).
 *
 * Call once from the root layout component. Returns a cleanup function that
 * removes the listener — use it as the useEffect return value to avoid leaks
 * on unmount.
 */
export function registerVisibilityHydrate(): () => void {
  if (typeof document === 'undefined') return () => {};
  const handler = () => {
    if (document.visibilityState === 'visible') {
      useJobStore.getState().hydrate();
    }
  };
  document.addEventListener('visibilitychange', handler);
  return () => {
    document.removeEventListener('visibilitychange', handler);
  };
}

// Reset job state on logout (see stores/session-reset). Registered here — not
// imported by auth-store — to avoid an auth-store <-> job-store import cycle
// (job-store reads auth state at call time above).
registerSessionReset(() => useJobStore.getState()._reset());

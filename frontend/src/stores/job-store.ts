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
import { createJob as apiCreateJob, listJobs as apiListJobs, cancelJob as apiCancelJob, getJob as apiGetJob } from '@/lib/api';
import { queryClient } from '@/lib/query-client';

/**
 * Per-kind query invalidation: when a job of the given kind reaches
 * `succeeded`, each listed query key is invalidated so the UI refetches
 * the new state (e.g. the freshly generated Pulse deck).
 *
 * Values are functions so paper_id etc. can be threaded through from payload.
 */
const INVALIDATE_ON_SUCCESS: Record<string, (job: Job) => string[][]> = {
  'pulse.generate':         () => [['pulse-today'], ['pulse-history'], ['pulse-stats']],
  'paper.process':          (j) => [['paper', String(j.payload?.paper_id)], ['action-items-unprocessed']],
  'card.generate':          () => [['decks'], ['cards']],
  'paper.analyze':          (j) => [['paper', String(j.payload?.paper_id)]],
  'papers.batch_process':   () => [['papers'], ['action-items-unprocessed']],
  'papers.batch_summarize': () => [['papers']],
  'extraction.batch':       () => [['extractions']],
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

interface JobStore {
  jobs: Record<string, Job>;
  /** AbortControllers for active SSE subscriptions — NOT persisted. */
  activeAborts: Record<string, AbortController>;

  /** POST a new job + subscribe to its SSE stream. Returns the job_id. */
  startJob: (kind: string, payload: unknown) => Promise<string>;
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

  // Internal helpers
  _upsertJob: (job: Job) => void;
  _cleanupSubscription: (jobId: string) => void;
}

export const useJobStore = create<JobStore>()(
  persist(
    (set, get) => ({
      jobs: {},
      activeAborts: {},

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

        // Add a placeholder job immediately so the UI reacts before SSE arrives
        const placeholder: Job = {
          id: job_id,
          kind,
          status: 'queued',
          progress: 0,
          progress_message: null,
          payload: payload as Record<string, unknown>,
          result: null,
          error: null,
          created_at: new Date().toISOString(),
          started_at: null,
          finished_at: null,
        };
        get()._upsertJob(placeholder);
        get().subscribe(job_id);
        return job_id;
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
            toast.success(`${job.kind} completed`);
            const keysFactory = INVALIDATE_ON_SUCCESS[job.kind];
            if (keysFactory) {
              for (const key of keysFactory(job)) {
                queryClient.invalidateQueries({ queryKey: key });
              }
            }
          } else if (job.status === 'failed') {
            const msg = job.error?.message ?? `${job.kind} failed`;
            const actionLink = job.error?.action_link;
            if (actionLink) {
              toast.error(msg, {
                action: {
                  label: actionLink.label,
                  onClick: () => {
                    window.location.href = actionLink.href;
                  },
                },
              });
            } else {
              toast.error(msg);
            }
          }
          get()._cleanupSubscription(jobId);
          setTimeout(() => {
            get().removeJob(jobId);
          }, EVICT_DELAY_MS);
        };

        // Stream job events via GET SSE endpoint
        (async () => {
          try {
            const res = await fetch(`/api/jobs/${jobId}/stream`, {
              method: 'GET',
              headers,
              signal: controller.signal,
            });

            if (!res.ok || !res.body) {
              // On auth failure, logout
              if (res.status === 401 || res.status === 403) {
                useAuthStore.getState().logout();
              }
              return;
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let terminalReceived = false;
            // Track current backoff delay; reset to base on every successful frame
            let currentReconnectDelay = reconnectDelay;

            while (true) {
              const { done, value } = await reader.read();
              if (done) {
                // Stream closed without a terminal event — fall back to a REST poll
                if (!terminalReceived) {
                  try {
                    const finalJob = await apiGetJob(jobId).catch(() => null);
                    if (finalJob) {
                      set((s) => ({ jobs: { ...s.jobs, [jobId]: finalJob } }));
                      if (TERMINAL_STATUSES.has(finalJob.status)) {
                        _handleTerminal(finalJob);
                      } else {
                        // Still running — re-subscribe with exponential backoff
                        get()._cleanupSubscription(jobId);
                        await sleep(currentReconnectDelay, controller.signal);
                        const nextDelay = Math.min(currentReconnectDelay * 2, RECONNECT_MAX_DELAY_MS);
                        get().subscribe(jobId, nextDelay);
                      }
                    }
                  } catch (err) {
                    if (err instanceof DOMException && err.name === 'AbortError') return;
                    /* best-effort */
                  }
                }
                break;
              }
              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() ?? '';

              for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const raw = line.slice(6).trim();
                if (raw === '[DONE]') break;
                try {
                  const event = JSON.parse(raw) as Partial<Job>;
                  // streaming_timeout sentinel — treat as non-terminal; reconnect with backoff
                  if ((event as { status?: string }).status === 'streaming_timeout') {
                    get()._cleanupSubscription(jobId);
                    await sleep(currentReconnectDelay, controller.signal);
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
            }

            await reader.cancel().catch(() => {});
          } catch (err) {
            // AbortError means we intentionally cancelled — not an error
            if (err instanceof DOMException && err.name === 'AbortError') return;
            // Other errors: clean up
            get()._cleanupSubscription(jobId);
          }
        })();
      },

      async cancelJob(jobId) {
        get()._cleanupSubscription(jobId);
        try {
          await apiCancelJob(jobId);
        } catch {
          /* best-effort */
        }
        // Optimistically update local status
        const job = get().jobs[jobId];
        if (job) {
          get()._upsertJob({ ...job, status: 'cancelled' });
          // Schedule eviction
          setTimeout(() => get().removeJob(jobId), EVICT_DELAY_MS);
        }
      },

      removeJob(jobId) {
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

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
import { createJob as apiCreateJob, listJobs as apiListJobs, cancelJob as apiCancelJob } from '@/lib/api';

/** Terminal statuses — job will not receive more events. */
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);

/** Delay before evicting terminal jobs from the store (ms). */
const EVICT_DELAY_MS = 5 * 60 * 1000; // 5 minutes

export interface Job {
  id: string;
  kind: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  progress: number;
  progress_message: string | null;
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
  subscribe: (jobId: string) => void;
  /** Cancel a running job. */
  cancelJob: (jobId: string) => Promise<void>;
  /** Remove a job from the store immediately (e.g. user dismisses). */
  removeJob: (jobId: string) => void;
  /** Returns true when a job of this kind is queued or running. */
  hasRunning: (kind: string) => boolean;
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

      subscribe(jobId) {
        // Avoid double-subscribing
        if (get().activeAborts[jobId]) return;

        const controller = new AbortController();
        set((state) => ({
          activeAborts: { ...state.activeAborts, [jobId]: controller },
        }));

        const apiKey = useAuthStore.getState().getApiKey();
        const headers: Record<string, string> = apiKey ? { 'X-API-Key': apiKey } : {};

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

            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() ?? '';

              for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const raw = line.slice(6).trim();
                if (raw === '[DONE]') break;
                try {
                  const event = JSON.parse(raw) as Partial<Job>;
                  const current = get().jobs[jobId] ?? {};
                  const updated: Job = {
                    ...(current as Job),
                    ...event,
                    id: jobId,
                  };
                  get()._upsertJob(updated);

                  if (TERMINAL_STATUSES.has(updated.status)) {
                    // Fire toast notification
                    if (updated.status === 'succeeded') {
                      toast.success(`${updated.kind} completed`);
                    } else if (updated.status === 'failed') {
                      const msg = updated.error?.message ?? `${updated.kind} failed`;
                      const actionLink = updated.error?.action_link;
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
                    // Clean up subscription
                    get()._cleanupSubscription(jobId);
                    // Schedule eviction after 5 minutes
                    setTimeout(() => {
                      get().removeJob(jobId);
                    }, EVICT_DELAY_MS);
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

      async hydrate() {
        try {
          const running = await apiListJobs({ status: 'running' });
          for (const job of running) {
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

// Async job queue: create, fetch, list, and cancel background jobs.
import { apiFetch } from './core';
import type { Job } from '@/stores/job-store';

export const createJob = (kind: string, payload: unknown): Promise<{ job_id: string; status: string }> =>
  apiFetch('/api/jobs', {
    method: 'POST',
    body: JSON.stringify({ kind, payload }),
  });

export const getJob = (jobId: string): Promise<Job> =>
  apiFetch<Job>(`/api/jobs/${jobId}`);

export const listJobs = (params?: { status?: string; kind?: string; limit?: number }): Promise<Job[]> => {
  const qs = new URLSearchParams();
  if (params?.status) qs.set('status', params.status);
  if (params?.kind) qs.set('kind', params.kind);
  if (params?.limit != null) qs.set('limit', String(params.limit));
  const query = qs.toString();
  return apiFetch<Job[]>(`/api/jobs${query ? `?${query}` : ''}`);
};

export const cancelJob = (jobId: string): Promise<void> =>
  apiFetch<void>(`/api/jobs/${jobId}/cancel`, { method: 'POST' });

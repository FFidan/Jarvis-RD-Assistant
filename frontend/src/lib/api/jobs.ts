// Async job queue: create, fetch, list, and cancel background jobs.
import { apiFetchJson, apiFetchVoid } from './core';
import { createJobResponseSchema, jobSchema } from './schemas/jobs';
import type { Job } from '@/stores/job-store';

export const createJob = (kind: string, payload: unknown): Promise<{ job_id: string; status: string }> =>
  apiFetchJson('/api/jobs', createJobResponseSchema, {
    method: 'POST',
    body: JSON.stringify({ kind, payload }),
  });

export const getJob = (jobId: string): Promise<Job> =>
  apiFetchJson(`/api/jobs/${jobId}`, jobSchema);

export const listJobs = (params?: { status?: string; kind?: string; limit?: number }): Promise<Job[]> => {
  const qs = new URLSearchParams();
  if (params?.status) qs.set('status', params.status);
  if (params?.kind) qs.set('kind', params.kind);
  if (params?.limit != null) qs.set('limit', String(params.limit));
  const query = qs.toString();
  return apiFetchJson(`/api/jobs${query ? `?${query}` : ''}`, jobSchema.array());
};

export const cancelJob = (jobId: string): Promise<void> =>
  apiFetchVoid(`/api/jobs/${jobId}/cancel`, { method: 'POST' });

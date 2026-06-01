// Daily Pulse deck: today's deck, history, rating, explanations, generation,
// stats, debug, and source health/history.
import { apiFetch, ApiError } from './core';
import type {
  PulseDeck,
  PulseRating,
  PulseStats,
  PulseDebugInfo,
  WhyExplanation,
  SourceHealth,
  SourceRunRecord,
} from '@/types';

/** Fetch today's Pulse deck. Returns `null` when the backend reports 404. */
export async function fetchPulseToday(): Promise<PulseDeck | null> {
  try {
    return await apiFetch<PulseDeck>('/api/pulse/today');
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export const fetchPulseHistory = (days = 30) =>
  apiFetch<PulseDeck[]>(`/api/pulse/history?days=${days}`);

export async function ratePulseCard(
  paperId: number,
  rating: PulseRating,
): Promise<void> {
  await apiFetch<{ status: string }>('/api/pulse/rate', {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId, rating }),
  });
}

export const explainPulseCard = (cardId: number) =>
  apiFetch<WhyExplanation>(`/api/pulse/explain/${cardId}`);

/**
 * Kick off a Pulse generation. Backend now returns `{job_id, status}` —
 * the deck is built asynchronously; consumers should poll `/api/jobs/{id}`
 * (or subscribe via the job store's SSE stream) for completion.
 */
export const generatePulseNow = () =>
  apiFetch<{ job_id: string; status: string }>('/api/pulse/generate', { method: 'POST' });

export const fetchPulseStats = (days = 30) =>
  apiFetch<PulseStats>(`/api/pulse/stats?days=${days}`);

export const fetchPulseDebug = () =>
  apiFetch<PulseDebugInfo>('/api/pulse/debug');

export const getPulseSourceHealth = () =>
  apiFetch<SourceHealth[]>('/api/pulse/source-health');

export const getPulseSourceHistory = (days = 7) =>
  apiFetch<Record<string, SourceRunRecord[]>>(`/api/pulse/source-history?days=${days}`);

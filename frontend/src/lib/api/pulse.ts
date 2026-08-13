// Daily Pulse deck: today's deck, history, rating, explanations, generation,
// stats, debug, and source health/history.
import { apiFetchJson, ApiError } from './core';
import {
  pulseDebugSchema,
  pulseDeckSchema,
  pulseExplainSchema,
  pulseGenerateResponseSchema,
  pulseRateResponseSchema,
  pulseStatsSchema,
  sourceHealthSchema,
  sourceHistorySchema,
} from './schemas/pulse';
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
    return await apiFetchJson('/api/pulse/today', pulseDeckSchema.nullable());
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export const fetchPulseHistory = (days = 30): Promise<PulseDeck[]> =>
  apiFetchJson(`/api/pulse/history?days=${days}`, pulseDeckSchema.array());

export async function ratePulseCard(
  paperId: number,
  rating: PulseRating,
): Promise<void> {
  await apiFetchJson('/api/pulse/rate', pulseRateResponseSchema, {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId, rating }),
  });
}

export const explainPulseCard = (cardId: number): Promise<WhyExplanation> =>
  apiFetchJson(`/api/pulse/explain/${cardId}`, pulseExplainSchema);

/**
 * Kick off a Pulse generation. Backend now returns `{job_id, status}` —
 * the deck is built asynchronously; consumers should poll `/api/jobs/{id}`
 * (or subscribe via the job store's SSE stream) for completion.
 */
export const generatePulseNow = (): Promise<{ job_id: string; status: 'queued' }> =>
  apiFetchJson('/api/pulse/generate', pulseGenerateResponseSchema, { method: 'POST' });

export const fetchPulseStats = (days = 30): Promise<PulseStats> =>
  apiFetchJson(`/api/pulse/stats?days=${days}`, pulseStatsSchema);

export const fetchPulseDebug = (): Promise<PulseDebugInfo> =>
  apiFetchJson('/api/pulse/debug', pulseDebugSchema);

export const getPulseSourceHealth = (): Promise<SourceHealth[]> =>
  apiFetchJson('/api/pulse/source-health', sourceHealthSchema.array());

export const getPulseSourceHistory = (days = 7): Promise<Record<string, SourceRunRecord[]>> =>
  apiFetchJson(`/api/pulse/source-history?days=${days}`, sourceHistorySchema);

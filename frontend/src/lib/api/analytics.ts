// Analytics dashboards and contradiction detection.
import { apiFetch } from './core';
import type {
  ActivityRow,
  RetentionRow,
  ReviewRow,
  LlmCostRow,
  SourceCountRow,
  StatusCountRow,
  AnalyticsSummaryResponse,
  PaperContradictionsResponse,
  ConsensusResponse,
  JobAccepted,
} from '@/types';

// --- Analytics ---
export const fetchAnalyticsActivity = (days?: number) =>
  apiFetch<ActivityRow[]>(`/api/analytics/activity${days ? `?days=${days}` : ''}`);
export const fetchAnalyticsRetention = (days?: number) =>
  apiFetch<RetentionRow[]>(`/api/analytics/retention${days ? `?days=${days}` : ''}`);
export const fetchAnalyticsReviews = (days?: number) =>
  apiFetch<ReviewRow[]>(`/api/analytics/reviews${days ? `?days=${days}` : ''}`);
export const fetchAnalyticsLlmCost = (days?: number) =>
  apiFetch<LlmCostRow[]>(`/api/analytics/llm-cost${days ? `?days=${days}` : ''}`);
export const fetchPapersBySource = () =>
  apiFetch<SourceCountRow[]>('/api/analytics/papers-by-source');
export const fetchPapersByStatus = () =>
  apiFetch<StatusCountRow[]>('/api/analytics/papers-by-status');
/**
 * Analytics "Reflect" KPI band — current/prior-period totals + streaks.
 * GET /api/analytics/summary?days=N (learning_engine analytics router).
 */
export const fetchAnalyticsSummary = (days?: number) =>
  apiFetch<AnalyticsSummaryResponse>(
    `/api/analytics/summary${days ? `?days=${days}` : ''}`,
  );

// --- Contradictions ---
export const fetchContradictions = (params?: {
  paper_id?: number;
  status?: string;
  limit?: number;
}) => {
  const qs = new URLSearchParams();
  if (params?.paper_id != null) qs.set('paper_id', String(params.paper_id));
  if (params?.status) qs.set('status', params.status);
  if (params?.limit != null) qs.set('limit', String(params.limit));
  const query = qs.toString();
  return apiFetch<PaperContradictionsResponse>(`/api/contradictions${query ? `?${query}` : ''}`);
};

export const scanContradictions = (body?: { paper_id?: number; limit?: number }) =>
  apiFetch<JobAccepted>('/api/contradictions/scan', {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });

export const scanPaperContradictions = (paperId: number, body?: { limit?: number }) =>
  apiFetch<JobAccepted>(`/api/papers/${paperId}/contradictions/scan`, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });

// --- Consensus ---
export const fetchConsensus = () => apiFetch<ConsensusResponse>('/api/consensus');

// Analytics dashboards and contradiction detection.
import { apiFetchJson } from './core';
import {
  activityRowSchema,
  analyticsSummarySchema,
  consensusResponseSchema,
  contradictionsResponseSchema,
  feedbackSummarySchema,
  jobAcceptedSchema,
  llmCostRowSchema,
  retentionRowSchema,
  reviewRowSchema,
  scanJobAcceptedSchema,
  sourceCountRowSchema,
  statusCountRowSchema,
} from './schemas/analytics';
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
  ScanJobAccepted,
  FeedbackSummary,
} from '@/types';

// --- Analytics ---
export const fetchAnalyticsActivity = (days?: number): Promise<ActivityRow[]> =>
  apiFetchJson(`/api/analytics/activity${days ? `?days=${days}` : ''}`, activityRowSchema.array());
export const fetchAnalyticsRetention = (days?: number): Promise<RetentionRow[]> =>
  apiFetchJson(`/api/analytics/retention${days ? `?days=${days}` : ''}`, retentionRowSchema.array());
export const fetchAnalyticsReviews = (days?: number): Promise<ReviewRow[]> =>
  apiFetchJson(`/api/analytics/reviews${days ? `?days=${days}` : ''}`, reviewRowSchema.array());
export const fetchAnalyticsLlmCost = (days?: number): Promise<LlmCostRow[]> =>
  apiFetchJson(`/api/analytics/llm-cost${days ? `?days=${days}` : ''}`, llmCostRowSchema.array());
export const fetchPapersBySource = (): Promise<SourceCountRow[]> =>
  apiFetchJson('/api/analytics/papers-by-source', sourceCountRowSchema.array());
export const fetchPapersByStatus = (): Promise<StatusCountRow[]> =>
  apiFetchJson('/api/analytics/papers-by-status', statusCountRowSchema.array());
export const fetchFeedbackSummary = (): Promise<FeedbackSummary> =>
  apiFetchJson('/api/analytics/feedback-summary', feedbackSummarySchema);
/**
 * Analytics "Reflect" KPI band — current/prior-period totals + streaks.
 * GET /api/analytics/summary?days=N (learning_engine analytics router).
 */
export const fetchAnalyticsSummary = (days?: number): Promise<AnalyticsSummaryResponse> =>
  apiFetchJson(
    `/api/analytics/summary${days ? `?days=${days}` : ''}`,
    analyticsSummarySchema,
  );

// --- Contradictions ---
export const fetchContradictions = (params?: {
  paper_id?: number;
  status?: string;
  limit?: number;
}): Promise<PaperContradictionsResponse> => {
  const qs = new URLSearchParams();
  if (params?.paper_id != null) qs.set('paper_id', String(params.paper_id));
  if (params?.status) qs.set('status', params.status);
  if (params?.limit != null) qs.set('limit', String(params.limit));
  const query = qs.toString();
  return apiFetchJson(
    `/api/contradictions${query ? `?${query}` : ''}`,
    contradictionsResponseSchema,
  );
};

export const scanContradictions = (body?: { paper_id?: number; limit?: number }): Promise<ScanJobAccepted> =>
  apiFetchJson('/api/contradictions/scan', scanJobAcceptedSchema, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });

export const scanPaperContradictions = (paperId: number, body?: { limit?: number }): Promise<JobAccepted> =>
  apiFetchJson(`/api/papers/${paperId}/contradictions/scan`, jobAcceptedSchema, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });

// --- Consensus ---
export const fetchConsensus = (): Promise<ConsensusResponse> =>
  apiFetchJson('/api/consensus', consensusResponseSchema);

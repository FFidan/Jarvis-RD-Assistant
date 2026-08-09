import { z } from 'zod';

export const activityRowSchema = z.looseObject({
  log_date: z.string(),
  tasks_completed: z.number(),
  cards_reviewed: z.number(),
  papers_read: z.number(),
  focus_hours: z.number(),
});

export const retentionRowSchema = z.looseObject({
  review_date: z.string(),
  total: z.number(),
  good_easy: z.number(),
  retention_pct: z.number().nullable(),
});

export const reviewRowSchema = z.looseObject({ rating: z.number(), count: z.number() });
export const llmCostRowSchema = z.looseObject({
  day: z.string(),
  total_cost: z.number(),
  workflow: z.string(),
});
export const sourceCountRowSchema = z.looseObject({
  source_type: z.string(),
  count: z.number(),
});
export const statusCountRowSchema = z.looseObject({ status: z.string(), count: z.number() });

const feedbackSummaryItemSchema = z.looseObject({
  paper_id: z.number(),
  title: z.string(),
  count: z.number(),
});
export const feedbackSummarySchema = z.looseObject({
  top_positive: z.array(feedbackSummaryItemSchema),
  top_negative: z.array(feedbackSummaryItemSchema),
});

export const analyticsSummarySchema = z.looseObject({
  papers_read_total: z.number(),
  focus_hours_total: z.number(),
  cards_reviewed_total: z.number(),
  papers_read_prev: z.number(),
  focus_hours_prev: z.number(),
  cards_reviewed_prev: z.number(),
  focus_streak_days: z.number(),
  cards_review_streak_days: z.number(),
});

export const contradictionSchema = z.looseObject({
  id: z.number(),
  paper_a_id: z.number(),
  paper_b_id: z.number(),
  paper_a_title: z.string(),
  paper_b_title: z.string(),
  finding_a: z.string(),
  finding_b: z.string(),
  quote_a: z.string(),
  quote_b: z.string(),
  page_a: z.number().nullable(),
  page_b: z.number().nullable(),
  contradiction_type: z.enum(['direct', 'methodological', 'result', 'interpretation']),
  explanation: z.string(),
  confidence: z.number(),
  status: z.enum(['verified', 'dismissed', 'false_positive']),
  created_at: z.string(),
});

export const contradictionsResponseSchema = z.looseObject({
  contradictions: z.array(contradictionSchema),
  total: z.number(),
});

export const scanJobAcceptedSchema = z.discriminatedUnion('status', [
  z.looseObject({ job_id: z.string(), status: z.literal('queued'), reason: z.null().optional() }),
  z.looseObject({ job_id: z.null(), status: z.literal('skipped'), reason: z.string() }),
]);

export const jobAcceptedSchema = z.looseObject({
  job_id: z.string(),
  status: z.literal('queued'),
  reason: z.null().optional(),
});

export const consensusAssessmentSchema = z.looseObject({
  stance: z.string(),
  paper_a_title: z.string(),
  paper_b_title: z.string(),
  quote_a: z.string(),
  quote_b: z.string(),
  page_a: z.number().nullable(),
  page_b: z.number().nullable(),
});

export const consensusResponseSchema = z.looseObject({
  claims: z.array(z.looseObject({
    claim_topic: z.string(),
    supports: z.number(),
    opposes: z.number(),
    paper_ids: z.array(z.number()),
    assessments: z.array(consensusAssessmentSchema),
  })),
  total: z.number(),
  truncated: z.boolean(),
});

import { z } from 'zod';

export const pulseSourceDiagnosticSchema = z.looseObject({
  status: z.string(),
  message: z.string(),
  status_code: z.number().nullable().optional(),
  retry_after_s: z.number().nullable().optional(),
  settings_hint: z.string().nullable().optional(),
});

export const pulseStaleDiagnosticSchema = z.looseObject({
  last_status: z.string().nullable(),
  cooldown_until: z.string().nullable(),
  consecutive_failures: z.number(),
});

export const pulseCardSchema = z.looseObject({
  card_id: z.number(),
  paper_id: z.number(),
  paper_title: z.string(),
  paper_authors: z.array(z.string()),
  paper_url: z.string().nullable(),
  rank: z.number(),
  score: z.number(),
  llm_relevance: z.number().nullable(),
  llm_novelty: z.number().nullable(),
  reasoning: z.string().nullable(),
  reasoning_verified: z.boolean().nullable(),
  reasoning_confidence: z.enum(['HIGH', 'MEDIUM', 'LOW', 'UNVERIFIED']).nullable(),
  signals: z.record(z.string(), z.number()),
  user_state: z.enum(['inbox', 'to_read', 'reading', 'done', 'trash']).nullable().optional(),
  tags: z.array(z.string()).nullable().optional(),
});

export const pulseDeckSchema = z.looseObject({
  deck_id: z.number(),
  deck_date: z.string(),
  card_count: z.number(),
  generated_at: z.string(),
  cards: z.array(pulseCardSchema),
  stats: z.looseObject({
    source_diagnostics: z.record(z.string(), pulseSourceDiagnosticSchema).optional(),
  }),
  degraded_reason: z.string().nullable().optional(),
  is_stale: z.boolean().optional(),
  stale_age_days: z.number().nullable().optional(),
  stale_diagnostics: z.record(z.string(), pulseStaleDiagnosticSchema).nullable().optional(),
  empty_reason: z.string().nullable().optional(),
});

export const pulseRateResponseSchema = z.looseObject({ status: z.literal('ok') });
export const pulseGenerateResponseSchema = z.looseObject({
  job_id: z.string(),
  status: z.literal('queued'),
});

export const pulseExplainSchema = z.looseObject({
  card_id: z.number(),
  reasoning: z.string().nullable(),
  signals: z.record(z.string(), z.number()),
  llm_relevance: z.number().nullable(),
  llm_novelty: z.number().nullable(),
});

export const pulseStatsSchema = z.looseObject({
  window_days: z.number(),
  decks_generated: z.number(),
  avg_candidates: z.number().nullable(),
  avg_llm_calls: z.number().nullable(),
  avg_duration_s: z.number().nullable(),
  last_run_at: z.string().nullable(),
  last_error: z.string().nullable(),
  degraded_reason: z.string().nullable(),
  has_learned_model: z.boolean().optional(),
});

export const pulseDebugSchema = z.looseObject({
  deck_date: z.string(),
  card_count: z.number(),
  degraded_reason: z.string().nullable(),
  source_counts: z.record(z.string(), z.number()),
  source_diagnostics: z.record(z.string(), pulseSourceDiagnosticSchema),
  topic_embeddings: z.array(z.looseObject({
    key: z.string(),
    dim: z.number().nullable(),
    ok: z.boolean(),
    non_null: z.boolean(),
  })),
  top_cards: z.array(z.looseObject({
    card_id: z.number(),
    paper_id: z.number(),
    title: z.string().nullable(),
    signals: z.record(z.string(), z.number()),
    final_score: z.number(),
    llm_relevance: z.number().nullable(),
    llm_novelty: z.number().nullable(),
  })),
  classifier_available: z.boolean(),
  classifier_sample_count: z.number().nullable(),
  classifier_feature_names: z.array(z.string()),
  classifier_auc: z.number().nullable(),
  classifier_auc_degradation_reason: z.string().nullable(),
  classifier_degradation_reason: z.string().nullable(),
});

export const sourceHealthSchema = z.looseObject({
  source_type: z.string(),
  last_request_at: z.string().nullable(),
  last_success_at: z.string().nullable(),
  last_status: z.string().nullable(),
  cooldown_until: z.string().nullable(),
  consecutive_failures: z.number(),
});

export const sourceRunRecordSchema = z.looseObject({
  source_type: z.string(),
  started_at: z.string(),
  finished_at: z.string().nullable(),
  status: z.string(),
  candidate_count: z.number(),
  duration_ms: z.number().nullable(),
});

export const sourceHistorySchema = z.record(z.string(), z.array(sourceRunRecordSchema));

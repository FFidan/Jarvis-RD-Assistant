import type { LifecycleState } from './paper';

export type PulseRating = 'up' | 'down' | 'save' | 'dismiss' | 'open';

export interface PulseCardItem {
  card_id: number;
  paper_id: number;
  paper_title: string;
  paper_authors: string[];
  paper_url: string | null;
  rank: number;
  score: number;
  llm_relevance: number | null;
  llm_novelty: number | null;
  reasoning: string | null;
  reasoning_verified: boolean | null;
  reasoning_confidence: 'HIGH' | 'MEDIUM' | 'LOW' | 'UNVERIFIED' | null;
  signals: Record<string, number>;
  user_state?: LifecycleState | null;
  tags?: string[] | null;
}

export interface PulseDeck {
  deck_id: number;
  deck_date: string;
  card_count: number;
  generated_at: string;
  cards: PulseCardItem[];
  stats: Record<string, unknown>;
  degraded_reason?: string | null;
  is_stale?: boolean;
  stale_age_days?: number | null;
  stale_diagnostics?: Record<string, unknown> | null;
  empty_reason?: string | null;
}

export interface SourceHealth {
  source_type: string;
  last_request_at: string | null;
  last_success_at: string | null;
  last_status: string | null;
  cooldown_until: string | null;
  consecutive_failures: number;
}

export interface SourceRunRecord {
  source_type: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  candidate_count: number;
  duration_ms: number | null;
}

export interface PulseStats {
  window_days: number;
  decks_generated: number;
  avg_candidates: number | null;
  avg_llm_calls: number | null;
  avg_duration_s: number | null;
  last_run_at: string | null;
  last_error: string | null;
  degraded_reason: string | null;
  has_learned_model?: boolean;
}

export interface WhyExplanation {
  card_id: number;
  reasoning: string | null;
  signals: Record<string, number>;
  llm_relevance: number | null;
  llm_novelty: number | null;
}

export interface PulseTopicEmbedding {
  key: string;
  dim: number | null;
  ok: boolean;
  non_null: boolean;
}

export interface PulseTopCard {
  card_id: number;
  paper_id: number;
  title: string;
  signals: Record<string, number>;
  final_score: number;
  llm_relevance: number | null;
  llm_novelty: number | null;
}

export interface PulseSourceDiagnostic {
  status: string;
  message: string;
  status_code: number | null;
  retry_after_s: number | null;
  settings_hint: string | null;
}

export interface PulseDebugInfo {
  deck_date: string;
  card_count: number;
  degraded_reason: string | null;
  source_counts: Record<string, number>;
  source_diagnostics: Record<string, PulseSourceDiagnostic>;
  topic_embeddings: PulseTopicEmbedding[];
  top_cards: PulseTopCard[];
  classifier_available: boolean;
  classifier_sample_count: number | null;
  classifier_feature_names: string[];
  classifier_auc: number | null;
  classifier_auc_degradation_reason: string | null;
  classifier_degradation_reason: string | null;
}

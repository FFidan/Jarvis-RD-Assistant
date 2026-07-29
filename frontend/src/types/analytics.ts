export interface ActivityRow {
  log_date: string;
  tasks_completed: number;
  cards_reviewed: number;
  papers_read: number;
  focus_hours: number;
  notes: string | null;
}

export interface RetentionRow {
  review_date: string;
  total: number;
  good_easy: number;
  retention_pct: number | null;
}

export interface ReviewRow {
  rating: number;
  count: number;
}

export interface LlmCostRow {
  day: string;
  total_cost: number;
  workflow: string;
}

export interface SourceCountRow {
  source_type: string;
  count: number;
}

export interface StatusCountRow {
  status: string;
  count: number;
}

export type ContradictionStatus = 'verified' | 'dismissed' | 'false_positive';

export interface PaperContradiction {
  id: number;
  paper_a_id: number;
  paper_b_id: number;
  paper_a_title: string;
  paper_b_title: string;
  finding_a: string;
  finding_b: string;
  quote_a: string;
  quote_b: string;
  page_a: number | null;
  page_b: number | null;
  contradiction_type: string;
  explanation: string;
  confidence: number;
  status: ContradictionStatus;
  created_at: string;
}

export interface PaperContradictionsResponse {
  contradictions: PaperContradiction[];
  total: number;
}

export interface ConsensusAssessment {
  stance: string;
  paper_a_title: string;
  paper_b_title: string;
  quote_a: string;
  quote_b: string;
  page_a: number | null;
  page_b: number | null;
}

export interface ConsensusClaim {
  claim_topic: string;
  supports: number;
  opposes: number;
  paper_ids: number[];
  assessments: ConsensusAssessment[];
}

export interface ConsensusResponse {
  claims: ConsensusClaim[];
  total: number;
  /** True when evidence was dropped before clustering, so `total` counts the
   *  clusters that survived a capped read rather than everything on record. */
  truncated: boolean;
}

// --- Weekly Digest ---

export interface WeeklyDigestTheme {
  theme: string;
  supporting_papers: number[];
  notes: string | null;
  verified: boolean | null;
  verification_reason: string | null;
}

export interface WeeklyDigestTopicPaper {
  paper_id: number;
  title: string;
  url: string | null;
  confidence: string | null;
  relevance_score: number | null;
}

export interface WeeklyDigestTopic {
  name: string;
  paper_count: number;
  themes: WeeklyDigestTheme[];
  top_papers: WeeklyDigestTopicPaper[];
  summary: string;
}

export interface WeeklyDigestResponse {
  topics: WeeklyDigestTopic[];
  total_papers: number;
  period_start: string;
  period_end: string;
}

export interface AnalyticsSummaryResponse {
  papers_read_total: number;
  focus_hours_total: number;
  cards_reviewed_total: number;
  papers_read_prev: number;
  focus_hours_prev: number;
  cards_reviewed_prev: number;
  focus_streak_days: number;
  cards_review_streak_days: number;
}

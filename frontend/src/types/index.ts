// NOTE: Types in this file are being migrated to OpenAPI-generated types.
// See ./generated.ts for the bridge file. Hand-written types here remain
// the source of truth until codegen is run and types are verified.

// =============================================================================
// Core TypeScript interfaces derived from db/init.sql and models.py
// =============================================================================

// --- Enums ---

export type SourceType = 'arxiv' | 'semantic_scholar' | 'openalex' | 'pubmed' | 'local';

export type Confidence = 'HIGH' | 'MEDIUM' | 'LOW';

export type PriorityLevel = 'must-read' | 'recommended' | 'background' | 'unscored';

// --- Core Domain ---

export interface Paper {
  id: number;
  external_id: string;
  source_type: SourceType;
  title: string;
  authors: string[];
  abstract: string | null;
  published_date: string | null;
  url: string;
  pdf_url: string | null;
  pdf_local_path: string | null;
  pdf_downloaded: boolean;
  citation_count: number;
  priority_score: number | null;
  metadata: Record<string, unknown>;
  discovered_at: string | null;
  created_at: string;
  // Phase A — migration 048 added discovery_origin; migration 049 + recommendation_feedback
  // joined surface produces recent_feedback. Optional because legacy callers may not have them.
  discovery_origin?: 'user_initiated' | 'pulse' | 'recommender' | 'citation_batch';
  recent_feedback?: RecentFeedback | null;
}

export interface KeyFinding {
  finding: string;
  quote: string;
  page_number: number | null;
  chunk_id: number | null;
  verified: boolean;
  snapshot_path: string | null;
}

export interface CrossReference {
  related_paper_id: number;
  relationship: string;
  explanation: string;
  related_quote: string | null;
}

export interface Summary {
  id: number;
  paper_id: number;
  summary_brief: string;
  summary_detailed: string;
  tldr: string | null;
  key_findings: KeyFinding[];
  methodology: string | null;
  limitations: string | null;
  relevance_notes: string | null;
  confidence: Confidence;
  cross_references: CrossReference[];
  llm_model: string | null;
  summary_verified: boolean;
  created_at: string;
}

export interface Chunk {
  id: number;
  paper_id: number;
  chunk_index: number;
  content: string;
  page_number: number | null;
  start_char: number | null;
  end_char: number | null;
  embedding_id: string | null;
  created_at: string;
}

export interface PaperDetail {
  paper: Paper;
  summary: Summary | null;
  chunks: Chunk[];
  user_state: UserState | null;
  has_project_links?: boolean;
}

// --- Feed ---

export interface FeedResponse {
  papers: FeedPaper[];
  total: number;
  search_mode?: string;
}

// --- Chat / Streaming ---

export interface Source {
  chunk_id?: number;
  paper_id?: number;
  paper_title?: string;
  content?: string;
  text?: string;
  page_number?: number | null;
  score: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
  confidence?: import('@/lib/sse').ConfidenceLevel;
  verified_fraction?: number;
  per_sentence?: { text: string; verified: boolean }[];
}

// --- Notes ---

export interface Note {
  id: number;
  paper_id: number;
  user_note: string;
  highlight_text: string | null;
  page_number: number | null;
  source: 'user' | 'zotero';
  zotero_annotation_key: string | null;
  verification_status: 'unverified' | 'verified' | 'failed';
  verified_quote: string | null;
  verified_page_number: number | null;
  promoted_at: string | null;
  created_at: string;
}

export interface JobAccepted {
  job_id: string;
  status: 'queued' | string;
}

// --- Topics ---

export interface Topic {
  id: number;
  name: string;
  query_terms: string[];
  category: string | null;
  description: string | null;
  enabled: boolean;
  created_at: string;
}

// --- Citation Graph ---

export interface GraphNode {
  id: number;
  title: string;
  citation_count: number;
  published_date: string | null;
  is_stub: boolean;
  display_size?: number;
}

export interface GraphEdge {
  source: number;
  target: number;
  is_influential: boolean | null;
  context: string | null;
}

export interface CitationGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

// --- Knowledge Graph ---

export interface Entity {
  id: number;
  name: string;
  canonical_name: string;
  entity_type: string;
  description: string | null;
  metadata: Record<string, unknown>;
  paper_count: number;
  created_at: string;
  display_size?: number;
}

export interface Relationship {
  id: number;
  source_entity_id: number;
  target_entity_id: number;
  relationship_type: string;
  paper_id: number | null;
  evidence_quote: string | null;
  confidence: number;
  created_at: string;
}

export interface KnowledgeGraph {
  entities: Entity[];
  relationships: Relationship[];
  entity_type_counts?: Record<string, number>;
}

// --- Tracked Authors ---

export interface TrackedAuthor {
  id: number;
  author_name: string;
  s2_author_id: string | null;
  source: string;
  enabled: boolean;
  last_checked_at: string | null;
  created_at: string;
}

// --- Dashboard Metrics ---

export interface DashboardMetrics {
  total_papers: number;
  unread_papers: number;
  pending_papers: number;
  due_cards: number;
  active_projects: number;
  topic_count: number;
  nudge_count: number;
  onboarding_stage?: string;
}

// --- Source Config ---

export interface SourceConfig {
  id: number;
  source_type: string;
  enabled: boolean;
  config: Record<string, unknown>;
  priority: number;
  display_order: number;
  created_at: string;
}

// --- Config Entry ---

export interface ConfigEntry {
  key: string;
  value: unknown;
}

// --- Nudges ---

export interface Nudge {
  id: number;
  nudge_type: string;
  cron_expression: string;
  enabled: boolean;
  config: Record<string, unknown>;
  last_fired_at: string | null;
  created_at: string;
}

// --- Setup / Pairing ---

export interface SetupStatus {
  setup_completed: boolean;
  models_ready: boolean;
  models_downloading: string[];
  topics_count: number;
  telegram_configured: boolean;
  telegram_paired: boolean;
}

export interface TelegramPairing {
  code: string;
  deep_link: string;
  expires_at: string; // ISO 8601
  bot_username_missing?: boolean;
}

export interface TelegramPairingStatus {
  paired: boolean;
  chat_id: number | null;
}

// --- Analytics ---

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

export type ContradictionStatus = 'candidate' | 'verified' | 'dismissed' | string;

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

// --- Extraction ---

export interface ExtractionField {
  name: string;
  label: string;
  description: string;
  type: string;
}

export interface ExtractionTemplate {
  id: number;
  name: string;
  description: string | null;
  fields: ExtractionField[];
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface ExtractedFieldValue {
  value: string | null;
  quote: string | null;
  verified: boolean;
  confidence: number;
  chunk_id: number | null;
  page_number: number | null;
}

export interface ExtractionTableRow {
  paper_id: number;
  paper_title: string;
  extractions: Record<string, ExtractedFieldValue>;
}

export interface BatchExtractionResponse {
  extracted: number;
  failed: number;
  skipped: number;
}

export interface PaperBrief {
  id: number;
  title: string;
  source_type?: string;
  published_date?: string | null;
}

// --- Projects ---

export type ProjectStatus = 'active' | 'paused' | 'completed' | 'archived';

export type TaskStatus = 'todo' | 'in_progress' | 'done' | 'blocked';

export interface Project {
  id: number;
  name: string;
  description: string | null;
  status: ProjectStatus;
  deadline: string | null;
  color: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail extends Project {
  total_tasks: number;
  done_tasks: number;
  total_milestones: number;
  completed_milestones: number;
}

export interface Task {
  id: number;
  project_id: number | null;
  parent_task_id: number | null;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: number;
  deadline: string | null;
  estimated_hours: number | null;
  actual_hours: number | null;
  sort_order: number;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Milestone {
  id: number;
  project_id: number;
  name: string;
  deadline: string | null;
  description: string | null;
  completed: boolean;
  completed_at: string | null;
  created_at: string;
}

// --- Executive / My Day ---

export interface MyDayTask {
  id: number;
  project_id: number | null;
  title: string;
  priority: number;
  deadline: string | null;
  status: TaskStatus;
  completed_at: string | null;
  project_name: string | null;
  project_color: string | null;
}

export interface ProjectPulseItem {
  id: number;
  name: string;
  total_tasks: number;
  done_tasks: number;
  next_milestone: string | null;
  next_milestone_deadline: string | null;
}

export interface MyDayResponse {
  tasks: MyDayTask[];
  cards_due: number;
  recommendations: Array<{
    recommendation_id: number;
    paper_id: number;
    score: number;
    title: string;
    authors: string[];
  }>;
  today_focus_hours: number;
  focus_streak_days: number;
  project_pulse: ProjectPulseItem[];
}

// --- Learning Cards ---

export interface Deck {
  id: number;
  name: string;
  description: string | null;
  topic_id: number | null;
  card_count: number;
  due_count: number;
  created_at: string;
}

export interface Evidence {
  quote: string | null;
  page_number: number | null;
  chunk_id: number | null;
  snapshot_path: string | null;
  verified: boolean;
}

export interface Card {
  id: number;
  deck_id: number;
  paper_id: number | null;
  card_type: string;
  front: string;
  back: string;
  evidence: Evidence | null;
  fsrs_state: Record<string, unknown>;
  due_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReviewResponse {
  card_id: number;
  rating: number;
  next_due_at: string;
  fsrs_state: Record<string, unknown>;
  review_log_id: number;
}

export interface RetentionStats {
  total_cards: number;
  due_now: number;
  reviewed_today: number;
  average_retention: number;
  reviews_by_rating: Record<string, number>;
  streak_days: number;
}

export interface GenerateCardsResponse {
  cards_created: number;
  cards: Card[];
  confidence: string;
}

/** Structured error from a job that failed with a JobError. */
export interface JobActionLink {
  label: string;
  href: string;
}

export interface JobErrorPayload {
  message: string;
  action_link?: JobActionLink;
}

/** A job row returned from GET /api/jobs/{id}. */
export interface JobRow {
  id: string;
  kind: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  progress: number | null;
  progress_message: string | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: JobErrorPayload | string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** Response from POST /api/generate (now queues a job). */
export interface GenerateJobAccepted {
  job_id: string;
  status: 'queued';
}

export interface ProjectPaper {
  id: number;
  title: string;
  authors: string[];
  source_type: string;
  published_date: string | null;
  notes: string | null;
  added_at: string;
}

// --- Discovery ---

export interface DiscoveryResult {
  paper_id: number;
  title: string;
  authors: string[];
  matching_snippet: string;
  similarity_score: number;
  url: string;
}

export interface SearchPreviewResult {
  external_id: string;
  source_type: string;
  title: string;
  authors: string[];
  abstract: string | null;
  published_date: string | null;
  url: string;
  pdf_url: string | null;
  citation_count: number;
  metadata: Record<string, unknown>;
  library_match: SearchPreviewLibraryMatch | null;
}

export interface SearchPreviewLibraryMatch {
  paper_id: number;
  has_project_links: boolean;
  zotero_item_key: string | null;
}

export interface SearchPreviewSourceError {
  kind: 'rate_limit' | 'api_error' | 'unavailable';
  message: string;
  status_code: number | null;
  retry_after_s: number | null;
  settings_hint: string | null;
}

export interface SearchPreviewResponse {
  results: SearchPreviewResult[];
  total: number;
  per_source_counts: Record<string, number>;
  degraded_sources: string[];
  source_errors: Record<string, SearchPreviewSourceError>;
}
// =============================================================================
// Phase A Lifecycle Redesign types (Wave 2.2 — legacy types removed)
// =============================================================================

// --- Phase A core enums ---

/** Phase A lifecycle state per spec §2 (post-2026-04-29 redesign). */
export type LifecycleState = 'inbox' | 'to_read' | 'reading' | 'done' | 'trash';

/** State that the paper had before being trashed (for restore). null when never trashed. */
export type StateBeforeTrash = 'inbox' | 'to_read' | 'reading' | 'done' | null;

// --- Phase A User State ---

/** User state per spec §9.1 (Phase A redesign). */
export interface UserState {
  state: LifecycleState;
  state_before_trash: StateBeforeTrash;
  starred: boolean;
  rating: number | null;
  user_notes: string | null;
  flagged: boolean;
  updated_at: string | null;
}

/** User-state envelope returned by lifecycle endpoints (spec §9.1). */
export interface UserStateResponse {
  state: LifecycleState;
  state_before_trash: StateBeforeTrash;
  starred: boolean;
  rating: number | null;
  user_notes: string | null;
  flagged: boolean;
  updated_at: string | null;
}

// --- Phase A Feedback ---

export interface RecentFeedback {
  signal: 'positive' | 'negative';
  source: 'pulse_thumbs' | 'feed_thumbs' | 'paper_detail_thumbs' | 'dismiss_combined';
  created_at: string;
}

// --- Phase A Paper Response ---

/** Canonical paper response per Phase A redesign (grounded against PaperResponse + PaperBase in models/papers.py). */
export interface LifecyclePaperResponse {
  id: number;
  external_id: string;
  source_type: SourceType;
  title: string;
  authors: string[];
  abstract: string | null;
  published_date: string | null;
  url: string | null;
  pdf_url: string | null;
  pdf_local_path: string | null;
  pdf_downloaded: boolean;
  discovered_at: string | null;
  priority_score: number | null;
  citation_count: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
  discovery_origin: 'user_initiated' | 'pulse' | 'recommender' | 'citation_batch';
  user_state: UserStateResponse | null;
  recent_feedback: RecentFeedback | null;
}

// --- Phase A Feed Paper ---

/**
 * Feed-level paper per Phase A redesign.
 * Grounded against FeedPaper (models/papers.py:290-307) which extends PaperResponse.
 * Fields: summary_brief, tldr, confidence, state, state_before_trash, starred, rating,
 *         priority_level, has_chunks, has_summary, recommendation_score,
 *         recommendation_reason, recommendation_modes, note_match_count, note_snippet.
 * Note: priority_score comes from PaperResponse (via PaperBase); user_status removed in Phase A.
 */
export interface FeedPaper extends LifecyclePaperResponse {
  state: LifecycleState;
  state_before_trash: StateBeforeTrash;
  starred: boolean;
  rating: number | null;
  summary_brief?: string | null;
  tldr?: string | null;
  confidence?: Confidence | null;
  priority_level?: string | null;
  has_chunks?: boolean;
  has_summary?: boolean;
  recommendation_score?: number | null;
  recommendation_reason?: string | null;
  recommendation_modes?: string[] | null;
  note_match_count?: number;
  note_snippet?: string | null;
}

// --- Phase A Bulk Actions ---

/**
 * Bulk action enum per BulkActionRequest in models/papers.py:582-597 (Phase A).
 */
export type BulkAction =
  | 'save' | 'skip' | 'trash'
  | 'mark_reading' | 'mark_done' | 'restore'
  | 'star' | 'unstar'
  | 'feedback_positive' | 'feedback_negative'
  | 'hard_delete';

// --- Phase A Surface / Filter Views ---

/** Library sub-chip filter per spec §5.4. */
export type LibraryFilter = 'starred' | 'reading' | 'to_read' | 'done';

/** Inbox source-type sub-chip filter — null means "all sources". */
export type InboxSourceFilter = 'arxiv' | 'semantic_scholar' | 'openalex' | 'pubmed';

/** Top-level feed surface per spec §5.4 (5 surfaces). */
export type SurfaceView = 'inbox' | 'library' | 'search' | 'ask' | 'trash';

// --- Phase A Feed Counts ---

/**
 * Feed counts per spec §6 — 10 named views.
 * Grounded against FeedCountsResponse in models/papers.py:600-612.
 */
export interface FeedCountsResponse {
  inbox: number;
  library: number;
  reading_list: number;
  reading: number;
  done: number;
  starred: number;
  trash: number;
  active: number;
  kept: number;
  all_non_trash: number;
}

// --- Phase A Feedback CRUD ---

export interface FeedbackListItem {
  paper_id: number;
  title: string;
  signal: 'positive' | 'negative';
  source: 'pulse_thumbs' | 'feed_thumbs' | 'paper_detail_thumbs' | 'dismiss_combined';
  reason: string | null;
  topic_id: number | null;
  topic_name: string | null;
  created_at: string;
}

export interface FeedbackListResponse {
  items: FeedbackListItem[];
  total: number;
}

export interface DeleteFeedbackResponse {
  deleted: number;
  topic_id: number;
}

// --- Citation Relation ---

export interface CitationRelation {
  source_paper_id: number;
  cited_paper_id: number;
  citation_context: string | null;
  is_influential: boolean | null;
  intent: string[];
}

// --- Priority Helper ---

export function priorityLevel(score: number | null): PriorityLevel {
  if (score === null) return 'unscored';
  if (score > 0.7) return 'must-read';
  if (score > 0.4) return 'recommended';
  return 'background';
}

// --- Pulse ---
//
// NOTE: the backend `PulseCardResponse` is lean — it ships flat `paper_title`,
// `paper_authors`, `paper_url` fields rather than a nested `paper` object.
// Frontend streams that need abstract / source_type / published_date should
// fetch full paper metadata separately via `getPaper(id)`.

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
  /** Current lifecycle state of the paper — used by Save button to enable unsave (to_read → inbox). */
  user_state?: LifecycleState | null;
}

export interface PulseDeck {
  deck_id: number;
  deck_date: string;
  card_count: number;
  generated_at: string;
  cards: PulseCardItem[];
  stats: Record<string, unknown>;
  degraded_reason?: string | null;
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

export interface PulseDebugInfo {
  deck_date: string;
  card_count: number;
  degraded_reason: string | null;
  source_counts: Record<string, number>;
  topic_embeddings: PulseTopicEmbedding[];
  top_cards: PulseTopCard[];
  classifier_available: boolean;
  classifier_sample_count: number | null;
  classifier_feature_names: string[];
  classifier_auc: number | null;
  classifier_auc_degradation_reason: string | null;
  classifier_degradation_reason: string | null;
}

export interface MissingFoundationalPaper {
  paper_id: number;
  title: string;
  authors: string[];
  year: number | null;
  citation_count: number;
  cited_by_library_count: number;
  url: string | null;
  pdf_available: boolean;
}

export interface FetchAndProcessFoundationalResponse {
  paper_id: number;
  status: 'queued' | 'no_pdf';
  job_id: string | null;
  message: string | null;
}

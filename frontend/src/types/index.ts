// NOTE: Types in this file are being migrated to OpenAPI-generated types.
// See ./generated.ts for the bridge file. Hand-written types here remain
// the source of truth until codegen is run and types are verified.

// =============================================================================
// Core TypeScript interfaces derived from db/init.sql and models.py
// =============================================================================

// --- Enums ---

export type SourceType = 'arxiv' | 'semantic_scholar' | 'local';

export type PaperStatus = 'new' | 'reading' | 'read' | 'archived' | 'starred';

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
  is_read: boolean;
  discovered_at: string | null;
  created_at: string;
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
}

// --- Feed ---

export interface FeedPaper extends Paper {
  summary_brief: string | null;
  tldr: string | null;
  confidence: Confidence | null;
  user_status: string | null;
  rating: number | null;
  priority_level?: string;
  has_chunks?: boolean;
  has_summary?: boolean;
  recommendation_score?: number | null;
  recommendation_reason?: string | null;
  recommendation_modes?: string[] | null;
}

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
}

// --- Notes ---

export interface Note {
  id: number;
  paper_id: number;
  user_note: string;
  highlight_text: string | null;
  page_number: number | null;
  created_at: string;
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
}
// --- User State ---

export interface UserState {
  status: string;
  rating: number | null;
  user_notes: string | null;
  flagged: boolean;
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
  signals: Record<string, number>;
}

export interface PulseDeck {
  deck_id: number;
  deck_date: string;
  card_count: number;
  generated_at: string;
  cards: PulseCardItem[];
  stats: Record<string, unknown>;
}

export interface PulseStats {
  window_days: number;
  decks_generated: number;
  avg_candidates: number | null;
  avg_llm_calls: number | null;
  avg_duration_s: number | null;
  last_run_at: string | null;
  last_error: string | null;
}

export interface WhyExplanation {
  card_id: number;
  reasoning: string | null;
  signals: Record<string, number>;
  llm_relevance: number | null;
  llm_novelty: number | null;
}

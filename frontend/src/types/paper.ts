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
  coverage?: number | null;
  passes?: number | null;
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
  processing_failed?: boolean;
}

// --- Feed ---

export interface FeedResponse {
  papers: FeedPaper[];
  total: number;
  search_mode?: string;
}

export type LifecycleState = 'inbox' | 'to_read' | 'reading' | 'done' | 'trash';

export type StateBeforeTrash = 'inbox' | 'to_read' | 'reading' | 'done' | null;

// --- User State ---

export interface UserState {
  state: LifecycleState;
  state_before_trash: StateBeforeTrash;
  starred: boolean;
  rating: number | null;
  user_notes: string | null;
  flagged: boolean;
  updated_at: string | null;
}

export type UserStateResponse = UserState;

// --- Feedback ---

export interface RecentFeedback {
  signal: 'positive' | 'negative';
  source: 'pulse_thumbs' | 'feed_thumbs' | 'paper_detail_thumbs' | 'dismiss_combined';
  created_at: string;
}

// --- Paper Response ---

export interface LifecyclePaperResponse {
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
  discovered_at: string | null;
  priority_score: number | null;
  citation_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  discovery_origin: 'user_initiated' | 'pulse' | 'recommender' | 'citation_batch';
  user_state: UserState | null;
  recent_feedback: RecentFeedback | null;
}

// --- Feed Paper ---

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

// --- Bulk Actions ---

export type BulkAction =
  | 'save' | 'skip' | 'trash'
  | 'mark_reading' | 'mark_done' | 'restore'
  | 'star' | 'unstar'
  | 'feedback_positive' | 'feedback_negative'
  | 'hard_delete';

// --- Surface / Filter Views ---

export type LibraryFilter = 'starred' | 'reading' | 'to_read' | 'done';

export type FeedScope = 'library' | 'corpus';

export type InboxSourceFilter = 'arxiv' | 'semantic_scholar' | 'openalex' | 'pubmed';

export type SurfaceView = 'inbox' | 'library' | 'search' | 'ask' | 'trash';

// --- Feed Counts ---

export interface TopicFacetCount {
  topic_id: number;
  name: string;
  count: number;
}

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

export interface FeedFacets {
  by_source: Record<string, number>;
  by_topic: TopicFacetCount[];
  untagged: number;
}

export type FeedCountsWithFacets = FeedCountsResponse & FeedFacets;

// --- Feedback CRUD ---

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

export interface ProjectPaper {
  id: number;
  title: string;
  authors: string[];
  source_type: string;
  published_date: string | null;
  notes: string | null;
  added_at: string;
}

export interface Topic {
  id: number;
  name: string;
  query_terms: string[];
  category: string | null;
  description: string | null;
  enabled: boolean;
  created_at: string;
}

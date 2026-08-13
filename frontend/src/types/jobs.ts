export interface JobAccepted {
  job_id: string;
  status: 'queued' | string;
}

/**
 * POST /api/contradictions/scan — the library-wide scan preflight may decide
 * not to queue a job at all (`status: 'skipped'`, `job_id: null`, with a
 * machine-readable `reason` such as 'no_findings').
 */
export interface ScanJobAccepted {
  job_id: string | null;
  status: 'queued' | 'skipped' | string;
  reason?: string | null;
}

export interface JobActionLink {
  label: string;
  href: string;
}

export interface JobErrorPayload {
  message: string;
  action_link?: JobActionLink;
}

export interface GenerateJobAccepted {
  job_id: string;
  status: 'queued';
}

export interface PartialGenJob {
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  error?: { message: string };
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

import type { JsonValue } from './json';

export interface ExtractedFieldValue {
  value: JsonValue;
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
  total: number;
  status: 'ok' | 'partial' | 'cancelled';
}

export interface PaperBrief {
  id: number;
  title: string;
  source_type?: string | null;
  published_date?: string | null;
}

// --- Citation / Knowledge Graph ---

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

export interface Entity {
  id: number;
  name: string;
  canonical_name: string;
  entity_type: string;
  description: string | null;
  metadata: Record<string, unknown>;
  paper_count: number;
  created_at: string | null;
  display_size?: number;
}

export interface Relationship {
  id: number;
  source_entity_id: number;
  target_entity_id: number;
  relationship_type: string;
  paper_id: number | null;
  page_number?: number | null;
  evidence_quote: string | null;
  confidence: number;
  created_at: string | null;
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

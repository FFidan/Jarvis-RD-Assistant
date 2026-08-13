import { z } from 'zod';
import { jsonObjectSchema, jsonValueSchema } from './common';

export const sourceTypeSchema = z.enum([
  'arxiv',
  'semantic_scholar',
  'local',
  'openalex',
  'pubmed',
  'zotero',
]);

export const confidenceSchema = z.enum(['NONE', 'HIGH', 'MEDIUM', 'LOW']);
export const discoveryOriginSchema = z.enum([
  'user_initiated',
  'pulse',
  'recommender',
  'citation_batch',
]);
export const lifecycleStateSchema = z.enum(['inbox', 'to_read', 'reading', 'done', 'trash']);
export const stateBeforeTrashSchema = z.enum(['inbox', 'to_read', 'reading', 'done']).nullable();
export const feedbackSourceSchema = z.enum([
  'pulse_thumbs',
  'feed_thumbs',
  'paper_detail_thumbs',
  'dismiss_combined',
]);

export const recentFeedbackSchema = z.looseObject({
  signal: z.enum(['positive', 'negative']),
  source: feedbackSourceSchema,
  created_at: z.string(),
});

export const paperSchema = z.looseObject({
  id: z.number(),
  external_id: z.string(),
  source_type: sourceTypeSchema,
  title: z.string(),
  authors: z.array(z.string()),
  abstract: z.string().nullable(),
  published_date: z.string().nullable(),
  url: z.string(),
  pdf_url: z.string().nullable(),
  pdf_local_path: z.string().nullable(),
  pdf_downloaded: z.boolean(),
  citation_count: z.number(),
  priority_score: z.number().nullable(),
  metadata: jsonObjectSchema,
  discovered_at: z.string().nullable(),
  created_at: z.string(),
  discovery_origin: discoveryOriginSchema,
  recent_feedback: recentFeedbackSchema.nullable().optional(),
});

export const userStateSchema = z.looseObject({
  state: lifecycleStateSchema,
  state_before_trash: stateBeforeTrashSchema,
  starred: z.boolean(),
  rating: z.number().nullable(),
  user_notes: z.string().nullable(),
  flagged: z.boolean(),
  updated_at: z.string().nullable(),
});

const keyFindingSchema = z.looseObject({
  finding: z.string(),
  quote: z.string(),
  page_number: z.number().nullable(),
  chunk_id: z.number().nullable(),
  verified: z.boolean(),
  snapshot_path: z.string().nullable(),
});

const crossReferenceSchema = z.looseObject({
  related_paper_id: z.number(),
  relationship: z.string(),
  explanation: z.string(),
  related_quote: z.string().nullable(),
  content_generation: z.number().optional(),
});

const summarySchema = z.looseObject({
  id: z.number(),
  paper_id: z.number(),
  summary_brief: z.string(),
  summary_detailed: z.string(),
  tldr: z.string().nullable(),
  key_findings: z.array(keyFindingSchema),
  methodology: z.string().nullable(),
  limitations: z.string().nullable(),
  relevance_notes: z.string().nullable(),
  confidence: confidenceSchema,
  cross_references: z.array(crossReferenceSchema),
  llm_model: z.string().nullable(),
  summary_verified: z.boolean(),
  created_at: z.string(),
  coverage: z.number().nullable().optional(),
  passes: z.number().nullable().optional(),
});

const chunkSchema = z.looseObject({
  id: z.number(),
  paper_id: z.number(),
  chunk_index: z.number(),
  content: z.string(),
  page_number: z.number().nullable(),
  start_char: z.number().nullable(),
  end_char: z.number().nullable(),
  embedding_id: z.string().nullable(),
  created_at: z.string(),
});

export const paperDetailSchema = z.looseObject({
  paper: paperSchema,
  summary: summarySchema.nullable(),
  chunks: z.array(chunkSchema),
  user_state: userStateSchema.nullable(),
  recent_feedback: recentFeedbackSchema.nullable().optional(),
  has_project_links: z.boolean().optional(),
  processing_failed: z.boolean().optional(),
});

export const feedPaperSchema = paperSchema.extend({
  state: lifecycleStateSchema,
  state_before_trash: stateBeforeTrashSchema,
  starred: z.boolean(),
  rating: z.number().nullable(),
  summary_brief: z.string().nullable().optional(),
  tldr: z.string().nullable().optional(),
  confidence: confidenceSchema.nullable().optional(),
  priority_level: z.string().nullable().optional(),
  has_chunks: z.boolean().optional(),
  has_summary: z.boolean().optional(),
  recommendation_score: z.number().nullable().optional(),
  recommendation_reason: z.string().nullable().optional(),
  recommendation_modes: z.array(z.string()).nullable().optional(),
  note_match_count: z.number().optional(),
  note_snippet: z.string().nullable().optional(),
});

export const feedResponseSchema = z.looseObject({
  papers: z.array(feedPaperSchema),
  total: z.number(),
  search_mode: z.string().optional(),
});

export const paperBriefSchema = z.looseObject({
  id: z.number(),
  title: z.string(),
  source_type: z.string().nullable().optional(),
  published_date: z.string().nullable().optional(),
});

const extractedFieldSchema = z.looseObject({
  value: jsonValueSchema,
  quote: z.string().nullable(),
  verified: z.boolean(),
  confidence: z.number(),
  chunk_id: z.number().nullable(),
  page_number: z.number().nullable(),
});

export const extractionTableRowSchema = z.looseObject({
  paper_id: z.number(),
  paper_title: z.string(),
  extractions: z.record(z.string(), extractedFieldSchema),
});

export const batchExtractionAcceptedSchema = z.looseObject({
  job_id: z.string(),
  total: z.number(),
});

const searchPreviewLibraryMatchSchema = z.looseObject({
  paper_id: z.number(),
  has_project_links: z.boolean(),
  zotero_item_key: z.string().nullable(),
});

export const searchPreviewResultSchema = z.looseObject({
  external_id: z.string(),
  source_type: sourceTypeSchema,
  title: z.string(),
  authors: z.array(z.string()),
  abstract: z.string().nullable(),
  published_date: z.string().nullable(),
  url: z.string(),
  pdf_url: z.string().nullable(),
  citation_count: z.number(),
  metadata: jsonObjectSchema,
  library_match: searchPreviewLibraryMatchSchema.nullable(),
});

const searchPreviewSourceErrorSchema = z.looseObject({
  kind: z.enum(['rate_limit', 'api_error', 'unavailable']),
  message: z.string(),
  status_code: z.number().nullable(),
  retry_after_s: z.number().nullable(),
  settings_hint: z.string().nullable(),
});

export const searchPreviewResponseSchema = z.looseObject({
  results: z.array(searchPreviewResultSchema),
  total: z.number(),
  per_source_counts: z.record(z.string(), z.number()),
  degraded_sources: z.array(z.string()),
  source_errors: z.record(z.string(), searchPreviewSourceErrorSchema),
});

export const lifecycleActionResponseSchema = z.looseObject({
  status: z.literal('ok'),
  paper_id: z.number(),
});
export const hardDeleteResponseSchema = z.looseObject({ deleted: z.number() });
export const bulkActionResponseSchema = z.looseObject({
  succeeded: z.array(z.number()),
  failed: z.array(z.looseObject({ paper_id: z.number(), error: z.string() })),
});

export const feedCountsSchema = z.looseObject({
  inbox: z.number(),
  library: z.number(),
  reading_list: z.number(),
  reading: z.number(),
  done: z.number(),
  starred: z.number(),
  trash: z.number(),
  active: z.number(),
  kept: z.number(),
  all_non_trash: z.number(),
  by_source: z.record(z.string(), z.number()),
  by_topic: z.array(z.looseObject({ topic_id: z.number(), name: z.string(), count: z.number() })),
  untagged: z.number(),
});

export const feedbackResponseSchema = z.looseObject({
  paper_id: z.number(),
  signal: z.enum(['positive', 'negative']),
  source: feedbackSourceSchema,
  created_at: z.string(),
});
const feedbackListItemSchema = z.looseObject({
  paper_id: z.number(),
  title: z.string(),
  signal: z.enum(['positive', 'negative']),
  source: feedbackSourceSchema,
  reason: z.string().nullable(),
  topic_id: z.number().nullable(),
  topic_name: z.string().nullable(),
  created_at: z.string(),
});
export const feedbackListResponseSchema = z.looseObject({
  items: z.array(feedbackListItemSchema),
  total: z.number(),
});
export const deleteFeedbackResponseSchema = z.looseObject({
  deleted: z.number(),
  topic_id: z.number(),
});

export const discoveryResultSchema = z.looseObject({
  paper_id: z.number(),
  title: z.string(),
  authors: z.array(z.string()),
  matching_snippet: z.string(),
  similarity_score: z.number(),
  url: z.string(),
});

export const queuedJobSchema = z.looseObject({
  job_id: z.string(),
  status: z.literal('queued'),
  reason: z.null().optional(),
});
export const processLibraryResponseSchema = z.discriminatedUnion('status', [
  queuedJobSchema,
  z.looseObject({
    job_id: z.null(),
    status: z.literal('skipped'),
    reason: z.string(),
  }),
]);
export const batchProcessResponseSchema = z.looseObject({
  queued: z.number(),
  total_unprocessed: z.number(),
  skipped_missing_pdf: z.number(),
  job_id: z.string().nullable(),
});
export const batchSummarizeResponseSchema = z.looseObject({
  total_unsummarized: z.number(),
  job_id: z.string().nullable(),
});

export const noteSchema = z.looseObject({
  id: z.number(),
  paper_id: z.number(),
  user_note: z.string(),
  highlight_text: z.string().nullable(),
  page_number: z.number().nullable(),
  source: z.enum(['user', 'zotero']),
  zotero_annotation_key: z.string().nullable(),
  verification_status: z.enum(['unverified', 'verified', 'failed']),
  verified_quote: z.string().nullable(),
  verified_page_number: z.number().nullable(),
  promoted_at: z.string().nullable(),
  stale: z.boolean(),
  created_at: z.string(),
});

export const missingFoundationalPaperSchema = z.looseObject({
  paper_id: z.number(),
  title: z.string(),
  authors: z.array(z.string()),
  year: z.number().nullable(),
  citation_count: z.number(),
  cited_by_library_count: z.number(),
  url: z.string().nullable(),
  pdf_available: z.boolean(),
});
export const fetchAndProcessFoundationalSchema = z.looseObject({
  paper_id: z.number(),
  status: z.enum(['queued', 'no_pdf']),
  job_id: z.string().nullable(),
  message: z.string().nullable(),
});

export const citationRelationSchema = z.looseObject({
  source_paper_id: z.number(),
  cited_paper_id: z.number(),
  citation_context: z.string().nullable(),
  is_influential: z.boolean().nullable(),
  intent: z.array(z.string()),
});
const graphNodeSchema = z.looseObject({
  id: z.number(),
  title: z.string(),
  citation_count: z.number(),
  published_date: z.string().nullable(),
  is_stub: z.boolean(),
  display_size: z.number().optional(),
});
const graphEdgeSchema = z.looseObject({
  source: z.number(),
  target: z.number(),
  is_influential: z.boolean().nullable(),
  context: z.string().nullable(),
});
export const citationGraphSchema = z.looseObject({
  nodes: z.array(graphNodeSchema),
  edges: z.array(graphEdgeSchema),
});
export const citationFetchResponseSchema = z.looseObject({
  citations_added: z.number(),
  references_added: z.number(),
  stubs_created: z.number(),
});

export const entitySchema = z.looseObject({
  id: z.number(),
  name: z.string(),
  canonical_name: z.string(),
  entity_type: z.string(),
  description: z.string().nullable(),
  metadata: jsonObjectSchema,
  paper_count: z.number(),
  created_at: z.string().nullable(),
  display_size: z.number().optional(),
});
const relationshipSchema = z.looseObject({
  id: z.number(),
  source_entity_id: z.number(),
  target_entity_id: z.number(),
  relationship_type: z.string(),
  paper_id: z.number().nullable(),
  page_number: z.number().nullable(),
  evidence_quote: z.string().nullable(),
  confidence: z.number(),
  created_at: z.string().nullable(),
});
export const knowledgeGraphSchema = z.looseObject({
  entities: z.array(entitySchema),
  relationships: z.array(relationshipSchema),
  entity_type_counts: z.record(z.string(), z.number()).optional(),
});
export const kgQueryResponseSchema = z.looseObject({
  results: z.array(jsonObjectSchema),
  query: z.string(),
});
export const entityExtractionResponseSchema = z.looseObject({
  entities_added: z.number(),
  relationships_added: z.number(),
  entities_merged: z.number(),
  dropped_relationships: z.number().optional(),
  saved_by_full_text_verify: z.number().optional(),
});
export const batchEntityExtractionResponseSchema = z.looseObject({
  extracted: z.number(),
  failed: z.number(),
  total: z.number(),
});

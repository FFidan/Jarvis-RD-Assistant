// Paper lifecycle: feed, search preview, save/skip/star/trash mutations,
// recommendation feedback, discovery, PDF upload/processing, paper detail,
// notes, foundational papers, citations, knowledge graph, and the extraction
// table + CSV export.
import type { FeedScope, LibraryFilter, SurfaceView } from '@/types';

import { apiFetchJson, apiFetchRaw, apiFetchVoid, triggerBlobDownload } from './core';
import {
  batchEntityExtractionResponseSchema,
  batchExtractionAcceptedSchema,
  batchProcessResponseSchema,
  batchSummarizeResponseSchema,
  bulkActionResponseSchema,
  citationFetchResponseSchema,
  citationGraphSchema,
  citationRelationSchema,
  deleteFeedbackResponseSchema,
  discoveryResultSchema,
  entityExtractionResponseSchema,
  entitySchema,
  extractionTableRowSchema,
  feedCountsSchema,
  feedbackListResponseSchema,
  feedbackResponseSchema,
  fetchAndProcessFoundationalSchema,
  feedResponseSchema,
  hardDeleteResponseSchema,
  kgQueryResponseSchema,
  knowledgeGraphSchema,
  lifecycleActionResponseSchema,
  missingFoundationalPaperSchema,
  noteSchema,
  paperBriefSchema,
  paperDetailSchema,
  paperSchema,
  processLibraryResponseSchema,
  queuedJobSchema,
  searchPreviewResponseSchema,
  userStateSchema,
} from './schemas/papers';
import type {
  Paper,
  PaperDetail,
  PaperBrief,
  ExtractionTableRow,
  SearchPreviewResponse,
  SearchPreviewResult,
  FeedResponse,
  FeedCountsWithFacets,
  DiscoveryResult,
  Note,
  CitationGraph,
  CitationRelation,
  KnowledgeGraph,
  Entity,
  JobAccepted,
  MissingFoundationalPaper,
  FetchAndProcessFoundationalResponse,
  UserState,
  BulkAction,
  FeedbackListResponse,
  DeleteFeedbackResponse,
  JsonValue,
} from '@/types';

// --- Extraction Table ---
export const fetchPapersBrief = (): Promise<PaperBrief[]> =>
  apiFetchJson('/api/papers/brief', paperBriefSchema.array());

export const searchPapersBrief = (search: string): Promise<PaperBrief[]> =>
  apiFetchJson(`/api/papers/brief?search=${encodeURIComponent(search)}`, paperBriefSchema.array());
export const fetchExtractionTable = (templateId: number, paperIds: number[]): Promise<ExtractionTableRow[]> =>
  apiFetchJson(
    `/api/extractions/table?template_id=${templateId}${paperIds.length ? `&paper_ids=${paperIds.join(',')}` : ''}`,
    extractionTableRowSchema.array(),
  );
export const batchExtract = (templateId: number, paperIds: number[]): Promise<{ job_id: string; total: number }> =>
  apiFetchJson('/api/extractions/batch', batchExtractionAcceptedSchema, {
    method: 'POST',
    body: JSON.stringify({ template_id: templateId, paper_ids: paperIds }),
  });

// --- Feed ---
export const fetchFeedPapers = (params: {
  unread_only?: boolean;
  sort?: 'discovered_at' | 'priority' | 'published_date' | 'title' | 'citation_count' | 'recommendation';
  limit?: number;
  offset?: number;
  q?: string;
  source_types?: string;
  topic_names?: string;
  date_from?: string;
  date_to?: string;
  recommended?: boolean;
  include_zotero_notes?: boolean;
  /** Named server-side view predicate, e.g. `all_non_trash`. */
  view?: string;
}): Promise<FeedResponse> => {
  const searchParams = new URLSearchParams();
  const { recommended, ...rest } = params;
  Object.entries(rest).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      searchParams.set(key, String(value));
    }
  });
  if (recommended) {
    searchParams.set('recommended', 'true');
  }
  return apiFetchJson(`/api/papers/feed?${searchParams.toString()}`, feedResponseSchema);
};


export interface SearchFilters {
  yearFrom?: number;
  yearTo?: number;
  sortBy?: 'relevance' | 'date';
  author?: string;
}

export const searchPreview = (
  query: string,
  sourceTypes?: string[],
  maxResults?: number,
  filters?: SearchFilters,
): Promise<SearchPreviewResponse> => {
  const source_types = sourceTypes ?? ['arxiv'];
  return apiFetchJson('/api/search-preview', searchPreviewResponseSchema, {
    method: 'POST',
    body: JSON.stringify({
      query,
      source_types,
      max_results: maxResults || 10,
      year_from: filters?.yearFrom ?? null,
      year_to: filters?.yearTo ?? null,
      sort_by: filters?.sortBy ?? 'relevance',
      author: filters?.author ?? null,
    }),
  });
};

export const batchSavePapers = (papers: SearchPreviewResult[] | Partial<Paper>[]): Promise<Paper[]> =>
  apiFetchJson('/api/papers/batch-save', paperSchema.array(), {
    method: 'POST',
    body: JSON.stringify(papers),
  });

// --- Paper lifecycle mutations ---

export async function savePaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/save`, lifecycleActionResponseSchema, { method: 'PUT' });
}

export async function restorePaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/restore`, lifecycleActionResponseSchema, { method: 'PUT' });
}

export async function hardDeletePaper(paperId: number): Promise<{ deleted: number }> {
  return apiFetchJson(`/api/papers/${paperId}`, hardDeleteResponseSchema, { method: 'DELETE' });
}

export async function bulkAction(body: { paper_ids: number[]; action: BulkAction }): Promise<{ succeeded: number[]; failed: { paper_id: number; error: string }[] }> {
  return apiFetchJson('/api/papers/bulk', bulkActionResponseSchema, { method: 'POST', body: JSON.stringify(body) });
}

export type BackendView =
  | 'inbox' | 'library' | 'reading_list' | 'reading' | 'done'
  | 'starred' | 'trash' | 'active' | 'kept' | 'all_non_trash';

export interface FeedCountSelection {
  scope?: FeedScope;
  view?: BackendView;
  source?: string | null;
  topicId?: number | null;
  untagged?: boolean;
}

/** Fetch feed counts conditioned on the active facet selection. */
export async function fetchFeedCounts(
  selection: FeedCountSelection = {},
): Promise<FeedCountsWithFacets> {
  const searchParams = new URLSearchParams();
  if (selection.scope) searchParams.set('scope', selection.scope);
  if (selection.view) searchParams.set('view', selection.view);
  if (selection.source) searchParams.set('source', selection.source);
  if (selection.topicId != null) searchParams.set('topic_id', String(selection.topicId));
  if (selection.untagged) searchParams.set('untagged', 'true');
  const query = searchParams.toString();
  return apiFetchJson(`/api/papers/feed/counts${query ? `?${query}` : ''}`, feedCountsSchema);
}

// --- Lifecycle mutations (additive) ---
// Return type for simple state transitions: { status: string; paper_id: number }

/** Skip a paper from the Inbox (state → done). */
export async function skipPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/skip`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Mark a paper as currently being read (state → reading). */
export async function markReading(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/reading`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Mark a paper as done/finished reading (state → done). */
export async function markDone(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/done`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Move a paper to the Trash (state → trash, saves state_before_trash). */
export async function trashPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/trash`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Trash the paper AND record negative feedback in one atomic transaction (source='dismiss_combined'). */
export async function trashAndRejectPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/trash_and_reject`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Set starred = TRUE on a paper. Does not change reading state. */
export async function starPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/star`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Set starred = FALSE on a paper. Does not change reading state. */
export async function unstarPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/unstar`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Body for PUT /api/papers/{id}/annotations. */
export interface AnnotationsBody {
  rating?: number | null;
  user_notes?: string | null;
  flagged?: boolean;
}

/** Update per-paper annotations (rating 1-5, user_notes, flagged). Returns the full user state. */
export async function upsertAnnotations(paperId: number, body: AnnotationsBody): Promise<UserState> {
  return apiFetchJson(`/api/papers/${paperId}/annotations`, userStateSchema, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

/** Body for POST /api/papers/{id}/feedback. */
export interface FeedbackBody {
  signal: 'positive' | 'negative';
  source: 'pulse_thumbs' | 'feed_thumbs' | 'paper_detail_thumbs' | 'dismiss_combined';
  reason?: string | null;
}

/** Submit per-paper recommendation feedback (positive/negative). */
export async function submitFeedback(
  paperId: number,
  body: FeedbackBody,
): Promise<{ paper_id: number; signal: 'positive' | 'negative'; source: string; created_at: string }> {
  return apiFetchJson(
    `/api/papers/${paperId}/feedback`,
    feedbackResponseSchema,
    { method: 'POST', body: JSON.stringify(body) },
  );
}

/**
 * Clear per-paper feedback (untoggle 👍/👎).
 * Hits DELETE /api/papers/:id/feedback?source=<source>.
 * Returns 204 on success.
 */
export async function clearFeedback(paperId: number, source: string): Promise<void> {
  await apiFetchVoid(`/api/papers/${paperId}/feedback?source=${encodeURIComponent(source)}`, {
    method: 'DELETE',
  });
}

/**
 * Unsave a paper — reverts state from `to_read` back to `inbox`.
 * Hits PUT /api/papers/:id/unsave.
 */
export async function unsavePaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetchJson(`/api/papers/${paperId}/unsave`, lifecycleActionResponseSchema, { method: 'PUT' });
}

/** Query params for GET /api/recommendation_feedback. */
export interface FetchRecommendationFeedbackParams {
  paper_id?: number;
  limit?: number;
  offset?: number;
}

/** List recommendation_feedback rows for the current user. */
export async function fetchRecommendationFeedback(
  params: FetchRecommendationFeedbackParams = {},
): Promise<FeedbackListResponse> {
  const qs = new URLSearchParams();
  if (params.paper_id !== undefined) qs.set('paper_id', String(params.paper_id));
  if (params.limit !== undefined) qs.set('limit', String(params.limit));
  if (params.offset !== undefined) qs.set('offset', String(params.offset));
  const suffix = qs.toString() ? `?${qs}` : '';
  return apiFetchJson(`/api/recommendation_feedback${suffix}`, feedbackListResponseSchema);
}

/** Bulk-delete recommendation_feedback rows for the given topic. */
export async function deleteRecommendationFeedback(topicId: number): Promise<DeleteFeedbackResponse> {
  return apiFetchJson(
    `/api/recommendation_feedback?topic_id=${topicId}`,
    deleteFeedbackResponseSchema,
    { method: 'DELETE' },
  );
}

/**
 * Backend view names per `queries/predicates.py::VIEW_PREDICATES` (10 values).
 * NOT the same as frontend `SurfaceView` (5 UI surfaces). When a user selects
 * `surface=library` + `filter=to_read`, the backend query needs `?view=reading_list`.
 */
const LIBRARY_FILTER_TO_BACKEND_VIEW: Record<
  LibraryFilter,
  BackendView
> = {
  starred: 'starred',
  reading: 'reading',
  to_read: 'reading_list',
  done: 'done',
};

function isLibraryFilter(value: string): value is LibraryFilter {
  return value in LIBRARY_FILTER_TO_BACKEND_VIEW;
}

/** Resolve a UI surface and optional library filter to its backend view. */
export function resolveFeedView(
  view: SurfaceView | undefined,
  filter: string | null | undefined,
  scope?: FeedScope,
): BackendView | undefined {
  if (view === 'library' && filter && isLibraryFilter(filter)) {
    return LIBRARY_FILTER_TO_BACKEND_VIEW[filter];
  }
  if (view === 'library' && scope === 'corpus') {
    return 'all_non_trash';
  }
  if (view === 'inbox' || view === 'library' || view === 'trash') {
    return view;
  }
  return undefined;
}

/** Surface-aware feed fetch for FeedView. Passes view= directly to the
 * backend so VIEW_PREDICATES (canonical lifecycle predicates) are used
 * — the legacy unread_only/statuses path uses different SQL that does
 * not match what the count badges show.
 *
 * filter is the Library sub-chip (starred/archived/reading) — when
 * provided it overrides the surface so e.g. surface=library + filter=
 * starred maps to view=starred.
 */
export async function fetchFeed(params: {
  view?: SurfaceView;
  filter?: string | null;
  scope?: FeedScope;
  limit?: number;
  offset?: number;
  sourceTypes?: string | null;
  topicId?: number | null;
  untagged?: boolean;
  q?: string;
}): Promise<FeedResponse> {
  const { view, filter, scope, limit = 30, offset = 0, sourceTypes, topicId, untagged, q } = params;

  const resolvedView = resolveFeedView(view, filter, scope);

  const searchParams = new URLSearchParams();
  if (resolvedView) {
    searchParams.set('view', resolvedView);
  }
  if (scope) {
    searchParams.set('scope', scope);
  }
  searchParams.set('limit', String(limit));
  searchParams.set('offset', String(offset));
  searchParams.set('include_zotero_notes', 'true');
  if (sourceTypes) {
    searchParams.set('source_types', sourceTypes);
  }
  if (topicId != null) {
    searchParams.set('topic_id', String(topicId));
  }
  if (untagged) {
    searchParams.set('untagged', 'true');
  }
  if (q) {
    searchParams.set('q', q);
  }
  return apiFetchJson(`/api/papers/feed?${searchParams.toString()}`, feedResponseSchema);
}

export const discoverPapers = (paperIds: number[], limit?: number): Promise<DiscoveryResult[]> =>
  apiFetchJson('/api/discover', discoveryResultSchema.array(), {
    method: 'POST',
    body: JSON.stringify({ paper_ids: paperIds, limit: limit || 10 }),
  });

export const scanLocalPdfs = (): Promise<JobAccepted> =>
  apiFetchJson('/api/scan-local-pdfs', queuedJobSchema, { method: 'POST' });

export async function uploadPdf(file: File, title: string): Promise<{ id: number; title: string }> {
  const form = new FormData();
  form.append('file', file);
  form.append('title', title);
  return apiFetchJson('/api/upload-pdf', paperSchema, { method: 'POST', body: form });
}

export const batchProcessPapers = (limit?: number): Promise<{ queued: number; total_unprocessed: number; skipped_missing_pdf: number; job_id: string | null }> =>
  apiFetchJson(
    `/api/papers/batch-process?limit=${limit || 10}`,
    batchProcessResponseSchema,
    { method: 'POST' },
  );


export const batchSummarizePapers = (limit?: number): Promise<{ total_unsummarized: number; job_id: string | null }> =>
  apiFetchJson(
    `/api/papers/batch-summarize?limit=${limit || 10}`,
    batchSummarizeResponseSchema,
    { method: 'POST' },
  );

/**
 * Enqueue a whole-library processing job (download → process → opt-in
 * summarize). Returns the JobCreateResponse envelope; ``job_id`` is null with
 * ``status: "skipped"`` when the library already needs no work.
 */
export const processLibrary = (summarize = false): Promise<{ job_id: string | null; status: 'queued' | 'skipped'; reason?: string | null }> =>
  apiFetchJson(
    `/api/papers/process-library?summarize=${summarize}`,
    processLibraryResponseSchema,
    { method: 'POST' },
  );

// --- Paper Detail ---
export const fetchPaperDetail = (paperId: number): Promise<PaperDetail> =>
  apiFetchJson(`/api/papers/${paperId}`, paperDetailSchema);

export const downloadPdf = (paperId: number): Promise<Paper> =>
  apiFetchJson(`/api/download-pdf/${paperId}`, paperSchema, { method: 'POST' });

export const processPdf = (paperId: number): Promise<JobAccepted> =>
  apiFetchJson(`/api/process-pdf/${paperId}`, queuedJobSchema, { method: 'POST' });

export const summarizePaper = (paperId: number, opts?: { force?: boolean }): Promise<JobAccepted> =>
  apiFetchJson(`/api/summarize/${paperId}`, queuedJobSchema, {
    method: 'POST',
    ...(opts?.force === true ? { body: JSON.stringify({ force: true }) } : {}),
  });

export const fetchNotes = (paperId: number, source?: 'user' | 'zotero'): Promise<Note[]> =>
  apiFetchJson(`/api/papers/${paperId}/notes${source ? `?source=${source}` : ''}`, noteSchema.array());

export const createNote = (paperId: number, data: { user_note: string; highlight_text?: string | null; page_number?: number | null }): Promise<Note> =>
  apiFetchJson(`/api/papers/${paperId}/notes`, noteSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const deleteNote = (noteId: number): Promise<void> =>
  apiFetchVoid(`/api/notes/${noteId}`, { method: 'DELETE' });

export const promoteZoteroNote = (noteId: number): Promise<Note> =>
  apiFetchJson(`/api/notes/${noteId}/promote`, noteSchema, { method: 'POST' });

export const zoteroSyncAnnotations = (paperId: number): Promise<JobAccepted> =>
  apiFetchJson(`/api/zotero/sync-annotations/${paperId}`, queuedJobSchema, { method: 'POST' });

export const fetchMissingFoundationalPapers = (): Promise<MissingFoundationalPaper[]> =>
  apiFetchJson('/api/analytics/missing-foundational', missingFoundationalPaperSchema.array());

export const fetchAndProcessFoundationalPaper = (paperId: number): Promise<FetchAndProcessFoundationalResponse> =>
  apiFetchJson('/api/analytics/fetch-and-process', fetchAndProcessFoundationalSchema, {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId }),
  });

// --- Citation Graph ---
export const fetchPaperCitations = (paperId: number): Promise<CitationRelation[]> =>
  apiFetchJson(`/api/citations/${paperId}`, citationRelationSchema.array());

export const getCitationGraph = (paperIds: number[], depth = 1): Promise<CitationGraph> => {
  const params = new URLSearchParams();
  paperIds.forEach((paperId) => params.append('paper_ids', String(Number(paperId))));
  params.set('depth', String(depth));
  return apiFetchJson(`/api/citations/graph?${params.toString()}`, citationGraphSchema);
};

export const fetchCitationsFromS2 = (paperId: number) =>
  apiFetchJson(
    `/api/citations/${paperId}/fetch`, citationFetchResponseSchema, { method: 'POST' }
  );

// --- Knowledge Graph ---
export const getKnowledgeGraph = (entityType?: string, minPaperCount?: number): Promise<KnowledgeGraph> => {
  const params = new URLSearchParams();
  if (entityType) params.set('entity_type', entityType);
  if (minPaperCount != null) params.set('min_paper_count', String(minPaperCount));
  return apiFetchJson(`/api/knowledge-graph?${params.toString()}`, knowledgeGraphSchema);
};

export const listKgEntities = (entityType?: string): Promise<Entity[]> =>
  apiFetchJson(`/api/knowledge-graph/entities${entityType ? `?entity_type=${entityType}` : ''}`, entitySchema.array());

export const queryKnowledgeGraph = (query: string): Promise<{ results: Array<Record<string, JsonValue>> }> =>
  apiFetchJson(`/api/knowledge-graph/query?q=${encodeURIComponent(query)}`, kgQueryResponseSchema, {
    method: 'GET',
  });

export const extractEntities = (paperId: number) =>
  apiFetchJson(`/api/extract-entities/${paperId}`, entityExtractionResponseSchema, {
    method: 'POST',
  });

export const batchExtractEntities = () =>
  apiFetchJson(
    '/api/extract-entities/batch',
    batchEntityExtractionResponseSchema,
    { method: 'POST' },
  );

// --- Extraction CSV Export ---
export async function downloadExtractionCsv(templateId: number): Promise<void> {
  const res = await apiFetchRaw(`/api/extractions/table?template_id=${templateId}&format=csv`);
  const blob = await res.blob();
  triggerBlobDownload(blob, 'extractions.csv');
}

// --- Citation Export (BibTeX / RIS) ---

export type CitationFormat = 'bibtex' | 'ris';

function filenameFromDisposition(res: Response, fallback: string): string {
  const disposition = res.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^"]+)"?/);
  return match && match[1] ? match[1] : fallback;
}

export async function downloadPaperCitation(paperId: number, format: CitationFormat): Promise<void> {
  const res = await apiFetchRaw(`/api/papers/${paperId}/citation?format=${format}`);
  const blob = await res.blob();
  const ext = format === 'bibtex' ? 'bib' : 'ris';
  triggerBlobDownload(blob, filenameFromDisposition(res, `paper_${paperId}.${ext}`));
}

export async function downloadPaperMarkdown(paperId: number): Promise<void> {
  const res = await apiFetchRaw(`/api/papers/${paperId}/export.md`);
  const blob = await res.blob();
  triggerBlobDownload(blob, filenameFromDisposition(res, `paper_${paperId}.md`));
}

export async function copyPaperCitation(paperId: number, format: CitationFormat): Promise<string> {
  const res = await apiFetchRaw(`/api/papers/${paperId}/citation?format=${format}`);
  return res.text();
}

function fetchBulkCitations(paperIds: number[], format: CitationFormat): Promise<Response> {
  return apiFetchRaw('/api/papers/citations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paper_ids: paperIds, format }),
  });
}

export async function downloadBulkCitations(paperIds: number[], format: CitationFormat): Promise<void> {
  const res = await fetchBulkCitations(paperIds, format);
  const blob = await res.blob();
  const ext = format === 'bibtex' ? 'bib' : 'ris';
  triggerBlobDownload(blob, filenameFromDisposition(res, `citations.${ext}`));
}

export async function copyBulkCitations(paperIds: number[], format: CitationFormat): Promise<string> {
  const res = await fetchBulkCitations(paperIds, format);
  return res.text();
}

// --- Snapshots ---

/**
 * Fetch a PDF page snapshot as a blob URL.
 *
 * Uses apiFetchRaw to include the X-API-Key header (native <img> requests
 * do not go through the auth interceptor).
 *
 * The caller is responsible for revoking the returned URL via
 * URL.revokeObjectURL() when the component unmounts.
 */
export async function fetchSnapshot(paperId: number, page: number): Promise<string> {
  const res = await apiFetchRaw(`/api/snapshots/${paperId}/${page}`);
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

/**
 * Fetch a paper's raw PDF as a blob URL for the in-PDF annotation reader.
 *
 * Uses apiFetchRaw so the X-API-Key header is attached (the PDF viewer loads
 * the document via fetch, not a native element that bypasses the interceptor).
 *
 * The caller is responsible for revoking the returned URL via
 * URL.revokeObjectURL() when the reader unmounts.
 */
export async function fetchPdfUrl(paperId: number): Promise<string> {
  const res = await apiFetchRaw(`/api/pdfs/${paperId}`);
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

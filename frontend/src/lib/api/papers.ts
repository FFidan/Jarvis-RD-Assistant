// Paper lifecycle: feed, search preview, save/skip/star/trash mutations,
// recommendation feedback, discovery, PDF upload/processing, paper detail,
// notes, foundational papers, citations, knowledge graph, and the extraction
// table + CSV export.
import { apiFetch, apiFetchRaw, triggerBlobDownload } from './core';
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
  UserStateResponse,
  BulkAction,
  FeedbackListResponse,
  DeleteFeedbackResponse,
} from '@/types';

// --- Extraction Table ---
export const fetchPapersBrief = () =>
  apiFetch<PaperBrief[]>('/api/papers/brief');

export const searchPapersBrief = (search: string) =>
  apiFetch<PaperBrief[]>(`/api/papers/brief?search=${encodeURIComponent(search)}`);
export const fetchExtractionTable = (templateId: number, paperIds: number[]) =>
  apiFetch<ExtractionTableRow[]>(
    `/api/extractions/table?template_id=${templateId}${paperIds.length ? `&paper_ids=${paperIds.join(',')}` : ''}`,
  );
export const batchExtract = (templateId: number, paperIds: number[]) =>
  apiFetch<{ job_id: string; total: number }>('/api/extractions/batch', {
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
}) => {
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
  return apiFetch<FeedResponse>(`/api/papers/feed?${searchParams.toString()}`);
};


export interface SearchFilters {
  yearFrom?: number;
  yearTo?: number;
  sortBy?: 'relevance' | 'date';
  author?: string;
}

export const searchPreview = (
  query: string,
  sourceTypes?: string | string[],
  maxResults?: number,
  filters?: SearchFilters,
) => {
  // Accept either a single source string (legacy) or an array of source types
  const source_types = Array.isArray(sourceTypes)
    ? sourceTypes
    : [sourceTypes || 'arxiv'];
  return apiFetch<SearchPreviewResponse>('/api/search-preview', {
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

export const batchSavePapers = (papers: SearchPreviewResult[] | Partial<Paper>[]) =>
  apiFetch<Paper[]>('/api/papers/batch-save', {
    method: 'POST',
    body: JSON.stringify(papers),
  });

// --- Paper lifecycle mutations ---

export async function savePaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch(`/api/papers/${paperId}/save`, { method: 'PUT' });
}

export async function restorePaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch(`/api/papers/${paperId}/restore`, { method: 'PUT' });
}

export async function hardDeletePaper(paperId: number): Promise<{ deleted: number }> {
  return apiFetch(`/api/papers/${paperId}`, { method: 'DELETE' });
}

export async function bulkAction(body: { paper_ids: number[]; action: BulkAction }): Promise<{ succeeded: number[]; failed: { paper_id: number; error: string }[] }> {
  return apiFetch('/api/papers/bulk', { method: 'POST', body: JSON.stringify(body) });
}

/**
 * Fetch feed counts for all surfaces.
 *
 * Always returns the full `FeedCountsWithFacets` payload which is structurally
 * compatible with the numeric-only `FeedCountsResponse` subset — existing
 * `keyof`-indexed consumers (e.g. `CountsBadge`) work unchanged.
 *
 * Pass `scope` to honour the active library/corpus scope for facet counts
 * (C-FACET-BE: backend `get_feed_counts` accepts ?scope= and passes it to
 * `fetch_feed_facet_counts`).
 */
export async function fetchFeedCounts(scope?: 'library' | 'corpus'): Promise<FeedCountsWithFacets> {
  const qs = scope ? `?scope=${scope}` : '';
  return apiFetch(`/api/papers/feed/counts${qs}`);
}

/** @deprecated Use `fetchFeedCounts(scope)` instead. */
export const fetchFeedCountsWithFacets = fetchFeedCounts;

// --- Lifecycle mutations (additive) ---
// Return type for simple state transitions: { status: string; paper_id: number }

/** Skip a paper from the Inbox (state → done). */
export async function skipPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch<{ status: string; paper_id: number }>(`/api/papers/${paperId}/skip`, { method: 'PUT' });
}

/** Mark a paper as currently being read (state → reading). */
export async function markReading(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch<{ status: string; paper_id: number }>(`/api/papers/${paperId}/reading`, { method: 'PUT' });
}

/** Mark a paper as done/finished reading (state → done). */
export async function markDone(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch<{ status: string; paper_id: number }>(`/api/papers/${paperId}/done`, { method: 'PUT' });
}

/** Move a paper to the Trash (state → trash, saves state_before_trash). */
export async function trashPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch<{ status: string; paper_id: number }>(`/api/papers/${paperId}/trash`, { method: 'PUT' });
}

/** Trash the paper AND record negative feedback in one atomic transaction (source='dismiss_combined'). */
export async function trashAndRejectPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch<{ status: string; paper_id: number }>(`/api/papers/${paperId}/trash_and_reject`, { method: 'PUT' });
}

/** Set starred = TRUE on a paper. Does not change reading state. */
export async function starPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch<{ status: string; paper_id: number }>(`/api/papers/${paperId}/star`, { method: 'PUT' });
}

/** Set starred = FALSE on a paper. Does not change reading state. */
export async function unstarPaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch<{ status: string; paper_id: number }>(`/api/papers/${paperId}/unstar`, { method: 'PUT' });
}

/** Body for PUT /api/papers/{id}/annotations. */
export interface AnnotationsBody {
  rating?: number | null;
  user_notes?: string | null;
  flagged?: boolean;
}

/** Update per-paper annotations (rating 1-5, user_notes, flagged). Returns the full user state. */
export async function upsertAnnotations(paperId: number, body: AnnotationsBody): Promise<UserStateResponse> {
  return apiFetch<UserStateResponse>(`/api/papers/${paperId}/annotations`, {
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
  return apiFetch<{ paper_id: number; signal: 'positive' | 'negative'; source: string; created_at: string }>(
    `/api/papers/${paperId}/feedback`,
    { method: 'POST', body: JSON.stringify(body) },
  );
}

/**
 * Clear per-paper feedback (untoggle 👍/👎).
 * Hits DELETE /api/papers/:id/feedback?source=<source>.
 * Returns 204 on success.
 */
export async function clearFeedback(paperId: number, source: string): Promise<void> {
  await apiFetchRaw(`/api/papers/${paperId}/feedback?source=${encodeURIComponent(source)}`, {
    method: 'DELETE',
  });
}

/**
 * Unsave a paper — reverts state from `to_read` back to `inbox`.
 * Hits PUT /api/papers/:id/unsave.
 */
export async function unsavePaper(paperId: number): Promise<{ status: string; paper_id: number }> {
  return apiFetch(`/api/papers/${paperId}/unsave`, { method: 'PUT' });
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
  return apiFetch<FeedbackListResponse>(`/api/recommendation_feedback${suffix}`);
}

/** Bulk-delete recommendation_feedback rows for the given topic. */
export async function deleteRecommendationFeedback(topicId: number): Promise<DeleteFeedbackResponse> {
  return apiFetch<DeleteFeedbackResponse>(
    `/api/recommendation_feedback?topic_id=${topicId}`,
    { method: 'DELETE' },
  );
}

/**
 * Backend view names per `queries/predicates.py::VIEW_PREDICATES` (10 values).
 * NOT the same as frontend `SurfaceView` (5 UI surfaces). When a user selects
 * `surface=library` + `filter=to_read`, the backend query needs `?view=reading_list`.
 */
type BackendView =
  | 'inbox' | 'library' | 'reading_list' | 'reading' | 'done'
  | 'starred' | 'trash' | 'active' | 'kept' | 'all_non_trash';

const LIBRARY_FILTER_TO_BACKEND_VIEW: Record<
  import('@/types').LibraryFilter,
  BackendView
> = {
  starred: 'starred',
  reading: 'reading',
  to_read: 'reading_list',
  done: 'done',
};

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
  view?: import('@/types').SurfaceView;
  filter?: string | null;
  scope?: import('@/types').FeedScope;
  limit?: number;
  offset?: number;
  sourceTypes?: string | null;
  topicId?: number | null;
  untagged?: boolean;
  q?: string;
}): Promise<FeedResponse> {
  const { view, filter, scope, limit = 30, offset = 0, sourceTypes, topicId, untagged, q } = params;

  // Map (surface=library, filter=X) → backend view name. Otherwise the surface
  // value itself is already a valid backend view (inbox/library/trash overlap).
  let resolvedView: BackendView | undefined;
  if (view === 'library' && filter && filter in LIBRARY_FILTER_TO_BACKEND_VIEW) {
    resolvedView = LIBRARY_FILTER_TO_BACKEND_VIEW[filter as import('@/types').LibraryFilter];
  } else if (view === 'library' && scope === 'corpus') {
    resolvedView = 'all_non_trash';
  } else if (view === 'inbox' || view === 'library' || view === 'trash') {
    resolvedView = view;
  }

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
  return apiFetch<FeedResponse>(`/api/papers/feed?${searchParams.toString()}`);
}

export const discoverPapers = (paperIds: number[], limit?: number) =>
  apiFetch<DiscoveryResult[]>('/api/discover', {
    method: 'POST',
    body: JSON.stringify({ paper_ids: paperIds, limit: limit || 10 }),
  });

export const scanLocalPdfs = () =>
  apiFetch<JobAccepted>('/api/scan-local-pdfs', { method: 'POST' });

export async function uploadPdf(file: File, title: string): Promise<{ id: number; title: string }> {
  const form = new FormData();
  form.append('file', file);
  form.append('title', title);
  return apiFetch('/api/upload-pdf', { method: 'POST', body: form });
}

export const batchProcessPapers = (limit?: number) =>
  apiFetch<{ queued: number; total_unprocessed: number; skipped_missing_pdf: number; job_id: string | null }>(
    `/api/papers/batch-process?limit=${limit || 10}`,
    { method: 'POST' },
  );


export const batchSummarizePapers = (limit?: number) =>
  apiFetch<{ total_unsummarized: number; job_id: string | null }>(
    `/api/papers/batch-summarize?limit=${limit || 10}`,
    { method: 'POST' },
  );

/**
 * Enqueue a whole-library processing job (download → process → opt-in
 * summarize). Returns the JobCreateResponse envelope; ``job_id`` is null with
 * ``status: "skipped"`` when the library already needs no work.
 */
export const processLibrary = (summarize = false) =>
  apiFetch<{ job_id: string | null; status: string; reason?: string | null }>(
    `/api/papers/process-library?summarize=${summarize}`,
    { method: 'POST' },
  );

// --- Paper Detail ---
export const fetchPaperDetail = (paperId: number) =>
  apiFetch<PaperDetail>(`/api/papers/${paperId}`);

export const downloadPdf = (paperId: number) =>
  apiFetch<Paper>(`/api/download-pdf/${paperId}`, { method: 'POST' });

export const processPdf = (paperId: number) =>
  apiFetch<{ job_id: string; status: string }>(`/api/process-pdf/${paperId}`, { method: 'POST' });

export const summarizePaper = (paperId: number, opts?: { force?: boolean }) =>
  apiFetch<JobAccepted>(`/api/summarize/${paperId}`, {
    method: 'POST',
    ...(opts?.force === true ? { body: JSON.stringify({ force: true }) } : {}),
  });

export const fetchNotes = (paperId: number, source?: 'user' | 'zotero') =>
  apiFetch<Note[]>(`/api/papers/${paperId}/notes${source ? `?source=${source}` : ''}`);

export const createNote = (paperId: number, data: { user_note: string; highlight_text?: string | null; page_number?: number | null }) =>
  apiFetch<Note>(`/api/papers/${paperId}/notes`, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const deleteNote = (noteId: number) =>
  apiFetch<{ status: string }>(`/api/notes/${noteId}`, { method: 'DELETE' });

export const promoteZoteroNote = (noteId: number) =>
  apiFetch<Note>(`/api/notes/${noteId}/promote`, { method: 'POST' });

export const zoteroSyncAnnotations = (paperId: number): Promise<JobAccepted> =>
  apiFetch(`/api/zotero/sync-annotations/${paperId}`, { method: 'POST' });

export const fetchMissingFoundationalPapers = () =>
  apiFetch<MissingFoundationalPaper[]>('/api/analytics/missing-foundational');

export const fetchAndProcessFoundationalPaper = (paperId: number) =>
  apiFetch<FetchAndProcessFoundationalResponse>('/api/analytics/fetch-and-process', {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId }),
  });

// --- Citation Graph ---
export const fetchPaperCitations = (paperId: number) =>
  apiFetch<CitationRelation[]>(`/api/citations/${paperId}`);

export const getCitationGraph = (paperIds: number[], depth = 1) => {
  const params = new URLSearchParams();
  paperIds.forEach((paperId) => params.append('paper_ids', String(Number(paperId))));
  params.set('depth', String(depth));
  return apiFetch<CitationGraph>(`/api/citations/graph?${params.toString()}`);
};

export const fetchCitationsFromS2 = (paperId: number) =>
  apiFetch<{ citations_added: number; references_added: number; stubs_created: number }>(
    `/api/citations/${paperId}/fetch`, { method: 'POST' }
  );

// --- Knowledge Graph ---
export const getKnowledgeGraph = (entityType?: string, minPaperCount?: number) => {
  const params = new URLSearchParams();
  if (entityType) params.set('entity_type', entityType);
  if (minPaperCount != null) params.set('min_paper_count', String(minPaperCount));
  return apiFetch<KnowledgeGraph>(`/api/knowledge-graph?${params.toString()}`);
};

export const listKgEntities = (entityType?: string) =>
  apiFetch<Entity[]>(`/api/knowledge-graph/entities${entityType ? `?entity_type=${entityType}` : ''}`);

export const queryKnowledgeGraph = (query: string) =>
  apiFetch<{ results: Array<Record<string, unknown>> }>(`/api/knowledge-graph/query?q=${encodeURIComponent(query)}`, {
    method: 'GET',
  });

export const extractEntities = (paperId: number) =>
  apiFetch<{ entities_added: number; relationships_added: number; entities_merged: number }>(`/api/extract-entities/${paperId}`, {
    method: 'POST',
  });

export const batchExtractEntities = () =>
  apiFetch<{ extracted: number; failed: number; total: number }>(
    '/api/extract-entities/batch',
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

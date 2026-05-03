/**
 * Fetch-based API client with X-API-Key authentication.
 * No axios dependency — uses the native fetch API.
 *
 * SECURITY: Every request includes the X-API-Key header from the auth store.
 * nginx does NOT inject API keys — the browser must send them.
 * On 401/403, the user is automatically logged out.
 */

import { useAuthStore } from '@/stores/auth-store';

/** Build auth headers from the current session API key. */
function authHeaders(): Record<string, string> {
  const apiKey = useAuthStore.getState().getApiKey();
  return apiKey ? { 'X-API-Key': apiKey } : {};
}

/** Auto-logout on authentication failure. */
function handleAuthFailure(status: number): void {
  if (status === 401 || status === 403) {
    useAuthStore.getState().logout();
  }
}

export class ApiError extends Error {
  public detail: string;
  constructor(public status: number, public body: string) {
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed.detail === 'string') {
        if (parsed.detail === 'Validation error' && Array.isArray(parsed.errors)) {
          const msgs = parsed.errors.map((e: { msg?: string }) => e.msg).filter(Boolean);
          detail = msgs.length > 0 ? msgs.join('; ') : parsed.detail;
        } else {
          detail = parsed.detail;
        }
      }
    } catch {
      if (body.includes('<html')) detail = `Server error (${status})`;
    }
    super(detail);
    this.name = 'ApiError';
    this.detail = detail;
  }
}

/**
 * Unified abort/error handler shared by apiFetch and apiFetchRaw.
 *
 * If the error is an AbortError and the timeout controller fired (not the
 * caller's own signal), we translate it into a friendly ApiError(0, …).
 * Caller-initiated aborts are re-thrown as-is so the caller can distinguish
 * them from timeouts.
 */
function _handleFetchError(
  err: unknown,
  timeoutController: AbortController,
  callerSignal?: AbortSignal | null,
): never {
  if (err instanceof DOMException && err.name === 'AbortError') {
    if (timeoutController.signal.aborted && !callerSignal?.aborted) {
      throw new ApiError(0, 'Request timed out — please try again');
    }
    throw err; // re-throw caller-initiated cancellations
  }
  throw err;
}

export async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 300_000); // 5 min
  // Combine caller signal with the 5-min timeout: abort on whichever fires first
  const signals = [controller.signal, init?.signal].filter(Boolean) as AbortSignal[];
  const combinedSignal = signals.length > 1 ? AbortSignal.any(signals) : signals[0];
  try {
    const res = await fetch(url, {
      ...init,
      signal: combinedSignal,
      headers: {
        ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...authHeaders(),
        ...init?.headers,
      },
    });
    if (!res.ok) {
      handleAuthFailure(res.status);
      throw new ApiError(res.status, await res.text());
    }
    if (res.status === 204) {
      return undefined as T;
    }
    return res.json();
  } catch (err) {
    _handleFetchError(err, controller, init?.signal);
  } finally {
    clearTimeout(timeoutId);
  }
}

/** Fetch that returns the raw Response (for blob downloads). */
export async function apiFetchRaw(url: string, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 300_000);
  // Combine caller signal with the 5-min timeout: abort on whichever fires first
  const signals = [controller.signal, init?.signal].filter(Boolean) as AbortSignal[];
  const combinedSignal = signals.length > 1 ? AbortSignal.any(signals) : signals[0];
  try {
    const res = await fetch(url, {
      ...init,
      signal: combinedSignal,
      headers: {
        ...authHeaders(),
        ...init?.headers,
      },
    });
    if (!res.ok) {
      handleAuthFailure(res.status);
      throw new ApiError(res.status, await res.text());
    }
    return res;
  } catch (err) {
    _handleFetchError(err, controller, init?.signal);
  } finally {
    clearTimeout(timeout);
  }
}

/** Health check helper — returns true if service responds ok. */
export async function checkHealth(path: string): Promise<boolean> {
  try {
    await apiFetch(path);
    return true;
  } catch {
    return false;
  }
}

// --- Imports for types ---
import type {
  DashboardMetrics,
  Topic,
  SourceConfig,
  TrackedAuthor,
  ConfigEntry,
  Nudge,
  ExtractionTemplate,
  ActivityRow,
  RetentionRow,
  ReviewRow,
  SearchPreviewResponse,
  LlmCostRow,
  SourceCountRow,
  StatusCountRow,
  PaperContradictionsResponse,
  ExtractionTableRow,
  PaperBrief,
  Project,
  ProjectDetail,
  Task,
  Milestone,
  ProjectPaper,
  Paper,
  Deck,
  SearchPreviewResult,
  Card,
  ReviewResponse,
  RetentionStats,
  GenerateCardsResponse,
  FeedResponse,
  DiscoveryResult,
  PaperDetail,
  Note,
  CitationGraph,
  CitationRelation,
  KnowledgeGraph,
  Entity,
  MyDayResponse,
  PulseDeck,
  PulseRating,
  PulseStats,
  PulseDebugInfo,
  JobAccepted,
  MissingFoundationalPaper,
  FetchAndProcessFoundationalResponse,
  WhyExplanation,
  SetupStatus,
  TelegramPairing,
  TelegramPairingStatus,
  GenerateJobAccepted,
  UserStateResponse,
  FeedCountsResponse,
  BulkAction,
  FeedbackListResponse,
  DeleteFeedbackResponse,
  JournalEntry,
  JournalPrompts,
} from '@/types';

// --- Dashboard ---
export const fetchDashboardMetrics = () =>
  apiFetch<DashboardMetrics>('/api/dashboard/metrics');

// --- Topics ---
export const fetchTopics = () => apiFetch<Topic[]>('/api/topics');
export const createTopic = (data: Partial<Topic>) =>
  apiFetch<Topic>('/api/topics', { method: 'POST', body: JSON.stringify(data) });
export const updateTopic = (id: number, data: Partial<Topic>) =>
  apiFetch<Topic>(`/api/topics/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTopic = (id: number) =>
  apiFetch<void>(`/api/topics/${id}`, { method: 'DELETE' });

// --- Sources ---
export const fetchSources = () => apiFetch<SourceConfig[]>('/api/sources');
export const updateSource = (id: number, data: Partial<SourceConfig>) =>
  apiFetch<SourceConfig>(`/api/sources/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const reorderSources = (source_types: string[]) =>
  apiFetch<SourceConfig[]>('/api/sources/reorder', {
    method: 'PATCH',
    body: JSON.stringify({ source_types }),
    headers: { 'Content-Type': 'application/json' },
  });

// --- Tracked Authors ---
export const fetchTrackedAuthors = () => apiFetch<TrackedAuthor[]>('/api/authors');
export const createTrackedAuthor = (data: Partial<TrackedAuthor>) =>
  apiFetch<TrackedAuthor>('/api/authors', { method: 'POST', body: JSON.stringify(data) });
export const updateTrackedAuthor = (id: number, data: Partial<TrackedAuthor>) =>
  apiFetch<TrackedAuthor>(`/api/authors/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTrackedAuthor = (id: number) =>
  apiFetch<void>(`/api/authors/${id}`, { method: 'DELETE' });
export const autoDetectAuthors = () =>
  apiFetch<{ added: number; already_tracked: number }>('/api/authors/auto-detect', { method: 'POST' });
export const checkTrackedAuthors = () =>
  apiFetch<{ new_papers: number; authors_checked: number }>('/api/authors/check', { method: 'POST' });

// --- Settings / Config ---
export const fetchConfig = () => apiFetch<ConfigEntry[]>('/api/config');
export const setConfig = (key: string, value: unknown) =>
  apiFetch<ConfigEntry>(`/api/config/${key}`, { method: 'PUT', body: JSON.stringify({ key, value }) });

// --- Setup / Pairing ---
export const getSetupStatus = () =>
  apiFetch<SetupStatus>('/api/system/setup-status');

export const createPairingCode = () =>
  apiFetch<TelegramPairing>('/api/telegram/pairing', { method: 'POST' });

export const getPairingStatus = () =>
  apiFetch<TelegramPairingStatus>('/api/telegram/pairing/status');

export const unpairTelegram = () =>
  apiFetch<void>('/api/config/telegram.owner_chat_id', {
    method: 'PUT',
    body: JSON.stringify({ key: 'telegram.owner_chat_id', value: null }),
  });

export const markSetupCompleted = () =>
  apiFetch<void>('/api/config/setup.completed', {
    method: 'PUT',
    body: JSON.stringify({ key: 'setup.completed', value: true }),
  });

// --- Nudges ---
export const fetchNudges = () => apiFetch<Nudge[]>('/api/nudges');
export const updateNudge = (id: number, data: Partial<Nudge>) =>
  apiFetch<Nudge>(`/api/nudges/${id}`, { method: 'PUT', body: JSON.stringify(data) });

// --- Extraction Templates ---
export const fetchExtractionTemplates = () =>
  apiFetch<ExtractionTemplate[]>('/api/extraction-templates');
export const createExtractionTemplate = (data: Partial<ExtractionTemplate>) =>
  apiFetch<ExtractionTemplate>('/api/extraction-templates', { method: 'POST', body: JSON.stringify(data) });
export const updateExtractionTemplate = (id: number, data: Partial<ExtractionTemplate>) =>
  apiFetch<ExtractionTemplate>(`/api/extraction-templates/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteExtractionTemplate = (id: number) =>
  apiFetch<void>(`/api/extraction-templates/${id}`, { method: 'DELETE' });

// --- Analytics ---
export const fetchAnalyticsActivity = (days?: number) =>
  apiFetch<ActivityRow[]>(`/api/analytics/activity${days ? `?days=${days}` : ''}`);
export const fetchAnalyticsRetention = (days?: number) =>
  apiFetch<RetentionRow[]>(`/api/analytics/retention${days ? `?days=${days}` : ''}`);
export const fetchAnalyticsReviews = (days?: number) =>
  apiFetch<ReviewRow[]>(`/api/analytics/reviews${days ? `?days=${days}` : ''}`);
export const fetchAnalyticsLlmCost = (days?: number) =>
  apiFetch<LlmCostRow[]>(`/api/analytics/llm-cost${days ? `?days=${days}` : ''}`);
export const fetchPapersBySource = () =>
  apiFetch<SourceCountRow[]>('/api/analytics/papers-by-source');
export const fetchPapersByStatus = () =>
  apiFetch<StatusCountRow[]>('/api/analytics/papers-by-status');

// --- Contradictions ---
export const fetchContradictions = (params?: {
  paper_id?: number;
  status?: string;
  limit?: number;
}) => {
  const qs = new URLSearchParams();
  if (params?.paper_id != null) qs.set('paper_id', String(params.paper_id));
  if (params?.status) qs.set('status', params.status);
  if (params?.limit != null) qs.set('limit', String(params.limit));
  const query = qs.toString();
  return apiFetch<PaperContradictionsResponse>(`/api/contradictions${query ? `?${query}` : ''}`);
};

export const scanContradictions = (body?: { paper_id?: number; limit?: number }) =>
  apiFetch<JobAccepted>('/api/contradictions/scan', {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });

export const scanPaperContradictions = (paperId: number, body?: { limit?: number }) =>
  apiFetch<JobAccepted>(`/api/papers/${paperId}/contradictions/scan`, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });

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

// --- Projects ---
export const fetchProjects = (status?: string) =>
  apiFetch<Project[]>(`/api/projects${status ? `?status=${status}` : ''}`);
export const fetchProjectDetail = (id: number) =>
  apiFetch<ProjectDetail>(`/api/projects/${id}`);
export const createProject = (data: {
  name: string;
  description?: string | null;
  status?: string;
  deadline?: string | null;
}) => apiFetch<Project>('/api/projects', { method: 'POST', body: JSON.stringify(data) });
export const updateProject = (id: number, data: Partial<Project>) =>
  apiFetch<Project>(`/api/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteProject = (id: number) =>
  apiFetch<void>(`/api/projects/${id}`, { method: 'DELETE' });

// --- Tasks ---
export const fetchTasks = (projectId: number) =>
  apiFetch<Task[]>(`/api/projects/${projectId}/tasks`);
export const createTask = (projectId: number, data: {
  title: string;
  description?: string | null;
  status?: string;
  priority?: number;
  deadline?: string | null;
}) => apiFetch<Task>(`/api/projects/${projectId}/tasks`, { method: 'POST', body: JSON.stringify(data) });
export const updateTask = (taskId: number, data: Partial<Task>) =>
  apiFetch<Task>(`/api/tasks/${taskId}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTask = (taskId: number) =>
  apiFetch<void>(`/api/tasks/${taskId}`, { method: 'DELETE' });

// --- Milestones ---
export const fetchMilestones = (projectId: number) =>
  apiFetch<Milestone[]>(`/api/projects/${projectId}/milestones`);
export const createMilestone = (projectId: number, data: {
  name: string;
  deadline?: string | null;
  description?: string | null;
}) => apiFetch<Milestone>(`/api/projects/${projectId}/milestones`, { method: 'POST', body: JSON.stringify(data) });
export const updateMilestone = (milestoneId: number, data: Partial<Milestone>) =>
  apiFetch<Milestone>(`/api/milestones/${milestoneId}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteMilestone = (milestoneId: number) =>
  apiFetch<void>(`/api/milestones/${milestoneId}`, { method: 'DELETE' });

// --- Project Papers ---
export const fetchProjectPapers = (projectId: number) =>
  apiFetch<ProjectPaper[]>(`/api/projects/${projectId}/papers`);
export const linkPaper = (projectId: number, paperId: number) =>
  apiFetch<{ project_id: number; paper_id: number }>(`/api/projects/${projectId}/papers/${paperId}`, { method: 'POST' });
export const unlinkPaper = (projectId: number, paperId: number) =>
  apiFetch<void>(`/api/projects/${projectId}/papers/${paperId}`, { method: 'DELETE' });
export const searchLibrary = (q: string) =>
  apiFetch<Paper[]>(`/api/papers?q=${encodeURIComponent(q)}&limit=20`);

// --- Decks ---
export const fetchDecks = () => apiFetch<Deck[]>('/api/decks');
export const createDeck = (data: { name: string; description?: string | null }) =>
  apiFetch<Deck>('/api/decks', { method: 'POST', body: JSON.stringify(data) });

// --- Cards ---
export const fetchCards = (deckId?: number) =>
  apiFetch<Card[]>(`/api/cards${deckId ? `?deck_id=${deckId}` : ''}`);
export const createCard = (data: {
  deck_id: number;
  card_type: string;
  front: string;
  back: string;
  paper_id?: number | null;
}) => apiFetch<Card>('/api/cards', { method: 'POST', body: JSON.stringify(data) });
export const updateCard = (id: number, data: Partial<Card>) =>
  apiFetch<Card>(`/api/cards/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteCard = (id: number) =>
  apiFetch<void>(`/api/cards/${id}`, { method: 'DELETE' });

// --- Review ---
export const getNextReview = (limit = 1) =>
  apiFetch<Card[]>(`/api/review/next?limit=${limit}`);
export const submitReview = (cardId: number, rating: number, durationMs?: number) =>
  apiFetch<ReviewResponse>(`/api/review/${cardId}`, {
    method: 'POST',
    body: JSON.stringify({ rating, review_duration_ms: durationMs ?? null }),
  });
export const getStats = () => apiFetch<RetentionStats>('/api/stats');

// --- Generate & Export ---

/** Enqueue card generation for a single paper. Returns a job_id to poll. */
export const generateCardsJob = (paperId: number, deckId: number, maxCards = 5) =>
  apiFetch<GenerateJobAccepted>('/api/generate', {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId, deck_id: deckId, max_cards: maxCards }),
  });

/** Kept as a deprecated alias so callers that haven't migrated yet still compile. */
export const generateCards = generateCardsJob as unknown as (
  paperId: number,
  deckId: number,
  maxCards?: number,
) => Promise<GenerateCardsResponse>;

function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export async function exportAnki(deckId: number): Promise<void> {
  const res = await apiFetchRaw(`/api/export/anki/${deckId}`);
  const blob = await res.blob();
  const disposition = res.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^"]+)"?/);
  const filename = match ? match[1] : `deck_${deckId}.apkg`;
  triggerBlobDownload(blob, filename);
}

// --- Feed ---
export const fetchFeedPapers = (params: {
  unread_only?: boolean;
  sort?: 'discovered_at' | 'priority' | 'published_date' | 'title' | 'citation_count' | 'recommendation';
  limit?: number;
  offset?: number;
  q?: string;
  statuses?: string;
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

// --- Recommendations ---
export interface RecommendationItem {
  paper_id: number;
  score: number;
  modes: string[];
  explanation: string;
  dismissed: boolean;
}

export const fetchRecommendations = (limit = 20) =>
  apiFetch<RecommendationItem[]>(`/api/recommendations?limit=${limit}`);

export const triggerRecommendationRefresh = () =>
  apiFetch<{ refreshed: number }>('/api/recommendations/refresh', { method: 'POST' });

export const dismissRecommendation = (paperId: number) =>
  apiFetch<{ dismissed: boolean }>(`/api/recommendations/${paperId}/dismiss`, { method: 'POST' });

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

// --- Paper lifecycle mutations (Phase A) ---

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

export async function fetchFeedCounts(): Promise<FeedCountsResponse> {
  return apiFetch('/api/papers/feed/counts');
}

// --- Phase A lifecycle mutations (Wave 2.1, additive) ---
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

/** Surface-aware feed fetch for FeedView. Passes view= directly to the
 * backend so VIEW_PREDICATES (canonical lifecycle predicates) are used
 * — the legacy unread_only/statuses path uses different SQL that does
 * not match what the count badges show.
 *
 * filter is the Library sub-chip (starred/archived/reading) — when
 * provided it overrides the surface so e.g. surface=library + filter=
 * starred maps to view=starred.
 */
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

export async function fetchFeed(params: {
  view?: import('@/types').SurfaceView;
  filter?: string | null;
  limit?: number;
  offset?: number;
  sourceTypes?: string | null;
}): Promise<FeedResponse> {
  const { view, filter, limit = 30, offset = 0, sourceTypes } = params;

  // Map (surface=library, filter=X) → backend view name. Otherwise the surface
  // value itself is already a valid backend view (inbox/library/trash overlap).
  let resolvedView: BackendView | undefined;
  if (view === 'library' && filter && filter in LIBRARY_FILTER_TO_BACKEND_VIEW) {
    resolvedView = LIBRARY_FILTER_TO_BACKEND_VIEW[filter as import('@/types').LibraryFilter];
  } else if (view === 'inbox' || view === 'library' || view === 'trash') {
    resolvedView = view;
  }

  const searchParams = new URLSearchParams();
  if (resolvedView) {
    searchParams.set('view', resolvedView);
  }
  searchParams.set('limit', String(limit));
  searchParams.set('offset', String(offset));
  searchParams.set('include_zotero_notes', 'true');
  if (sourceTypes) {
    searchParams.set('source_types', sourceTypes);
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
  apiFetch<{ queued: number; total_unprocessed: number; skipped_missing_pdf: number; job_id: string | null }>('/api/papers/batch-process', {
    method: 'POST',
    body: JSON.stringify({ limit: limit || 10 }),
  });

export const processPapersBatch = (paperIds: number[]) =>
  apiFetch<{ job_id: string; status: string }>('/api/papers/process_batch', {
    method: 'POST',
    body: JSON.stringify({ paper_ids: paperIds }),
  });

export const batchSummarizePapers = (limit?: number) =>
  apiFetch<{ total_unsummarized: number; job_id: string | null }>(
    `/api/papers/batch-summarize?limit=${limit || 10}`,
    { method: 'POST' },
  );

// --- Paper Detail ---
export const fetchPaperDetail = (paperId: number) =>
  apiFetch<PaperDetail>(`/api/papers/${paperId}`);

export const downloadPdf = (paperId: number) =>
  apiFetch<Paper>(`/api/download-pdf/${paperId}`, { method: 'POST' });

export const processPdf = (paperId: number) =>
  apiFetch<{ job_id: string; status: string }>(`/api/process-pdf/${paperId}`, { method: 'POST' });

export const summarizePaper = (paperId: number) =>
  apiFetch<JobAccepted>(`/api/summarize/${paperId}`, { method: 'POST' });

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

export const batchFetchCitations = () =>
  apiFetch<{ queued: number; message: string }>('/api/citations/batch-fetch', {
    method: 'POST',
  });

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

// --- Executive / My Day ---

export const fetchMyDay = () =>
  apiFetch<MyDayResponse>('/api/executive/my-day');

export const createQuickTask = (data: { title: string; project_id?: number | null; priority?: number }) =>
  apiFetch<Task>('/api/executive/tasks', {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const logFocusSession = (data: { duration_hours: number; task_id?: number; paper_id?: number }) =>
  apiFetch<{ status: string; recorded_hours: number }>('/api/executive/focus/log', {
    method: 'POST',
    body: JSON.stringify(data),
  });

// --- Pulse ---

/** Fetch today's Pulse deck. Returns `null` when the backend reports 404. */
export async function fetchPulseToday(): Promise<PulseDeck | null> {
  try {
    return await apiFetch<PulseDeck>('/api/pulse/today');
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export const fetchPulseHistory = (days = 30) =>
  apiFetch<PulseDeck[]>(`/api/pulse/history?days=${days}`);

export async function ratePulseCard(
  paperId: number,
  rating: PulseRating,
): Promise<void> {
  await apiFetch<{ status: string }>('/api/pulse/rate', {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId, rating }),
  });
}

export const explainPulseCard = (cardId: number) =>
  apiFetch<WhyExplanation>(`/api/pulse/explain/${cardId}`);

/**
 * Kick off a Pulse generation. Backend now returns `{job_id, status}` —
 * the deck is built asynchronously; consumers should poll `/api/jobs/{id}`
 * (or subscribe via the job store's SSE stream) for completion.
 */
export const generatePulseNow = () =>
  apiFetch<{ job_id: string; status: string }>('/api/pulse/generate', { method: 'POST' });

export const fetchPulseStats = (days = 30) =>
  apiFetch<PulseStats>(`/api/pulse/stats?days=${days}`);

export const fetchPulseDebug = () =>
  apiFetch<PulseDebugInfo>('/api/pulse/debug');

import type { Job } from '@/stores/job-store';

export const createJob = (kind: string, payload: unknown): Promise<{ job_id: string; status: string }> =>
  apiFetch('/api/jobs', {
    method: 'POST',
    body: JSON.stringify({ kind, payload }),
  });

export const getJob = (jobId: string): Promise<Job> =>
  apiFetch<Job>(`/api/jobs/${jobId}`);

export const listJobs = (params?: { status?: string; kind?: string; limit?: number }): Promise<Job[]> => {
  const qs = new URLSearchParams();
  if (params?.status) qs.set('status', params.status);
  if (params?.kind) qs.set('kind', params.kind);
  if (params?.limit != null) qs.set('limit', String(params.limit));
  const query = qs.toString();
  return apiFetch<Job[]>(`/api/jobs${query ? `?${query}` : ''}`);
};

export const cancelJob = (jobId: string): Promise<void> =>
  apiFetch<void>(`/api/jobs/${jobId}/cancel`, { method: 'POST' });

/**
 * Stream a job's SSE events via GET.
 *
 * Calls `onEvent` for each progress update until the job reaches a terminal
 * status or the signal is aborted.
 */
// --- Zotero ---

export async function zoteroTest(): Promise<{ success: boolean; error?: string }> {
  return apiFetch('/api/zotero/test', { method: 'POST' });
}

export async function zoteroPushPaper(paperId: number): Promise<{ job_id: string; status: string }> {
  return apiFetch(`/api/papers/${paperId}/zotero`, { method: 'POST' });
}

export async function zoteroGetLinkage(paperId: number): Promise<{
  zotero_item_key: string | null;
  zotero_citation_key: string | null;
  zotero_last_pushed_at: string | null;
}> {
  return apiFetch(`/api/papers/${paperId}/zotero`);
}

export async function zoteroResync(paperId: number): Promise<{ job_id: string; status: string }> {
  return apiFetch(`/api/zotero/resync/${paperId}`, { method: 'POST' });
}

export async function zoteroPollNow(): Promise<{ job_id: string; status: string }> {
  return apiFetch('/api/zotero/poll', { method: 'POST' });
}

// --- Cloud LLM Providers ---

export type CloudProvider = 'anthropic' | 'openai' | 'google';

/**
 * Returns the masked key value for each cloud provider (e.g. "sk-a****").
 * Null if no key is stored.
 */
export async function getProviderStatuses(): Promise<Record<CloudProvider, string | null>> {
  const configs = await apiFetch<Array<{ key: string; value: unknown }>>('/api/config');
  const result: Record<CloudProvider, string | null> = { anthropic: null, openai: null, google: null };
  for (const provider of ['anthropic', 'openai', 'google'] as CloudProvider[]) {
    const entry = configs.find((c) => c.key === `llm.${provider}.api_key`);
    if (entry != null && entry.value != null) {
      const v = entry.value;
      result[provider] = typeof v === 'string' ? v.replace(/^"|"$/g, '') : String(v);
    }
  }
  return result;
}

/** Save a cloud provider API key via the unified config endpoint. */
export async function setProviderKey(provider: CloudProvider, apiKey: string): Promise<void> {
  await setConfig(`llm.${provider}.api_key`, apiKey);
}

/** Test connectivity for a cloud provider. */
export async function testProvider(
  provider: CloudProvider,
): Promise<{ ok: boolean; error: string | null }> {
  return apiFetch(`/api/providers/${provider}/test`, { method: 'POST' });
}

// --- Jobs (streaming) ---

export async function streamJob(
  jobId: string,
  onEvent: (ev: {
    progress?: number;
    status?: string;
    progress_message?: string | null;
    result?: Record<string, unknown> | null;
    error?: { message: string; action_link?: { label: string; href: string } } | null;
  }) => void,
  signal: AbortSignal,
): Promise<void> {
  const apiKey = useAuthStore.getState().getApiKey();
  const res = await fetch(`/api/jobs/${jobId}/stream`, {
    method: 'GET',
    headers: apiKey ? { 'X-API-Key': apiKey } : {},
    signal,
  });

  if (!res.ok) {
    handleAuthFailure(res.status);
    throw new ApiError(res.status, await res.text());
  }

  if (!res.body) return;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') return;
        try {
          onEvent(JSON.parse(raw));
        } catch {
          /* skip malformed frames */
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
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

// --- My Day Journal ---

export async function getJournalEntry(date: string): Promise<JournalEntry | null> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 300_000);
  try {
    const res = await fetch(`/api/my-day/journal?date=${date}`, {
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
    });
    handleAuthFailure(res.status);
    if (res.status === 404) return null;
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json();
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function upsertJournalEntry(date: string, prompts: JournalPrompts): Promise<JournalEntry> {
  return apiFetch<JournalEntry>('/api/my-day/journal', {
    method: 'POST',
    body: JSON.stringify({ date, prompts }),
  });
}

// --- React Query hooks ---
import { useQuery } from '@tanstack/react-query';

export function useFeedCounts() {
  return useQuery({
    queryKey: ['feed-counts'],
    queryFn: fetchFeedCounts,
    staleTime: 5_000,
  });
}

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
  LlmCostRow,
  SourceCountRow,
  StatusCountRow,
  ExtractionTableRow,
  BatchExtractionResponse,
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
  UserState,
  Summary,
  Note,
  CitationGraph,
  CitationRelation,
  KnowledgeGraph,
  Entity,
  MyDayResponse,
  PulseDeck,
  PulseRating,
  PulseStats,
  WhyExplanation,
  SetupStatus,
  TelegramPairing,
  TelegramPairingStatus,
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
  apiFetch<BatchExtractionResponse>('/api/extractions/batch', {
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
export const generateCards = (paperId: number, deckId: number, maxCards = 5) =>
  apiFetch<GenerateCardsResponse>('/api/generate', {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId, deck_id: deckId, max_cards: maxCards }),
  });

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
  source?: string,
  maxResults?: number,
  filters?: SearchFilters,
) =>
  apiFetch<SearchPreviewResult[]>('/api/search-preview', {
    method: 'POST',
    body: JSON.stringify({
      query,
      source: source || 'arxiv',
      max_results: maxResults || 10,
      year_from: filters?.yearFrom ?? null,
      year_to: filters?.yearTo ?? null,
      sort_by: filters?.sortBy ?? 'relevance',
      author: filters?.author ?? null,
    }),
  });

export const batchSavePapers = (papers: SearchPreviewResult[] | Partial<Paper>[]) =>
  apiFetch<Paper[]>('/api/papers/batch-save', {
    method: 'POST',
    body: JSON.stringify(papers),
  });

export const markPaperRead = (paperId: number) =>
  apiFetch<{ status: string }>(`/api/papers/${paperId}/read`, { method: 'PUT' });

export const discoverPapers = (paperIds: number[], limit?: number) =>
  apiFetch<DiscoveryResult[]>('/api/discover', {
    method: 'POST',
    body: JSON.stringify({ paper_ids: paperIds, limit: limit || 10 }),
  });

export const scanLocalPdfs = () =>
  apiFetch<{ imported: number; skipped: number; scanned: number }>('/api/scan-local-pdfs', { method: 'POST' });

export async function uploadPdf(file: File, title: string): Promise<{ id: number; title: string }> {
  const form = new FormData();
  form.append('file', file);
  form.append('title', title);
  return apiFetch('/api/upload-pdf', { method: 'POST', body: form });
}

export const batchProcessPapers = (limit?: number) =>
  apiFetch<{ queued: number; total_unprocessed: number; skipped_missing_pdf: number }>('/api/papers/batch-process', {
    method: 'POST',
    body: JSON.stringify({ limit: limit || 10 }),
  });

export const batchSummarizePapers = (limit?: number) =>
  apiFetch<{ summarized: number; failed: number; total_unsummarized: number }>(
    `/api/papers/batch-summarize?limit=${limit || 10}`,
    { method: 'POST' },
  );

// --- Paper Detail ---
export const fetchPaperDetail = (paperId: number) =>
  apiFetch<PaperDetail>(`/api/papers/${paperId}`);

export const upsertUserState = (paperId: number, data: Partial<UserState>) =>
  apiFetch<UserState>(`/api/papers/${paperId}/user-state`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });

export const downloadPdf = (paperId: number) =>
  apiFetch<Paper>(`/api/download-pdf/${paperId}`, { method: 'POST' });

export const processPdf = (paperId: number) =>
  apiFetch<{ paper_id: number; chunk_count: number; status: string }>(`/api/process-pdf/${paperId}`, { method: 'POST' });

export const summarizePaper = (paperId: number) =>
  apiFetch<Summary>(`/api/summarize/${paperId}`, { method: 'POST' });

export const fetchNotes = (paperId: number) =>
  apiFetch<Note[]>(`/api/papers/${paperId}/notes`);

export const createNote = (paperId: number, data: { user_note: string; highlight_text?: string | null; page_number?: number | null }) =>
  apiFetch<Note>(`/api/papers/${paperId}/notes`, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const deleteNote = (noteId: number) =>
  apiFetch<{ status: string }>(`/api/notes/${noteId}`, { method: 'DELETE' });

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

export const generatePulseNow = () =>
  apiFetch<PulseDeck>('/api/pulse/generate', { method: 'POST' });

export const fetchPulseStats = (days = 30) =>
  apiFetch<PulseStats>(`/api/pulse/stats?days=${days}`);

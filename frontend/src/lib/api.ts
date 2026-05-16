/**
 * Fetch-based API client with X-API-Key authentication.
 * No axios dependency — uses the native fetch API.
 *
 * SECURITY: Every request includes the X-API-Key header from the auth store.
 * nginx does NOT inject API keys — the browser must send them.
 * On 401 (auth invalid / expired), the user is logged out + toasted. 403
 * (permission denied for an authenticated user) does NOT trigger logout —
 * it surfaces as a per-request error so role-gated routes don't bounce the
 * whole session.
 */

import { toast } from 'sonner';
import { useAuthStore } from '@/stores/auth-store';

/** Build auth headers from the current session API key. */
function authHeaders(): Record<string, string> {
  const apiKey = useAuthStore.getState().getApiKey();
  return apiKey ? { 'X-API-Key': apiKey } : {};
}

let _sessionExpiredToastShownAt = 0;

/** Auto-logout on genuine auth failure (401 only). */
function handleAuthFailure(status: number): void {
  if (status !== 401) return;
  if (!useAuthStore.getState().isAuthenticated) return;
  // Debounce: a burst of parallel requests can all 401 at once; show one toast.
  const now = Date.now();
  if (now - _sessionExpiredToastShownAt > 5000) {
    _sessionExpiredToastShownAt = now;
    toast.error('Session expired — please sign in again.', { duration: 6000 });
  }
  useAuthStore.getState().logout();
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
      // WS-2A: send the jarvis_session HttpOnly cookie on every API call so
      // the backend SessionMiddleware can populate request.state.user_id.
      // 'include' (not 'same-origin') so cross-origin dev setups (Vite on
      // :5173 hitting backend on :3001) still carry the cookie.
      credentials: init?.credentials ?? 'include',
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
      // WS-2A: same rationale as apiFetch — carry the jarvis_session cookie.
      credentials: init?.credentials ?? 'include',
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

export type ServiceHealthStatus = 'ok' | 'degraded' | 'down' | 'unknown';

export interface ServiceHealth {
  name: string;
  label: string;
  status: ServiceHealthStatus;
}

export interface StackHealthSummary {
  services: ServiceHealth[];
  /** Number of services with status 'degraded' */
  degradedCount: number;
  /** Number of services with status 'down' */
  downCount: number;
  /** Overall rollup: ok / degraded / down */
  overall: ServiceHealthStatus;
}

/**
 * Fetch full health status for all stack components.
 *
 * Calls public endpoints for service-level status (paper_ingestion,
 * learning_engine) and the authenticated internal endpoint for
 * per-dependency breakdown (postgres, qdrant, ollama, litellm, vector).
 *
 * Individual fetch failures are mapped to 'down' so a single unreachable
 * service never throws — callers always get a StackHealthSummary.
 */
export async function fetchStackHealth(): Promise<StackHealthSummary> {
  // Dependency statuses from paper_ingestion internal health endpoint
  const depLabels: Record<string, string> = {
    postgres: 'PostgreSQL',
    qdrant: 'Qdrant',
    litellm: 'LiteLLM',
    ollama: 'Ollama',
    vector: 'Vector',
  };

  let depChecks: Record<string, string> = {};
  try {
    const internal = await apiFetch<{ status: string; checks: Record<string, string> }>(
      '/health/paper_ingestion/internal',
    );
    depChecks = internal.checks ?? {};
  } catch {
    // If internal endpoint is unreachable, mark all deps as unknown
    for (const key of Object.keys(depLabels)) depChecks[key] = 'unknown';
  }

  // Service-level status from public health endpoints
  const [piOk, leOk] = await Promise.all([
    checkHealth('/health/paper_ingestion'),
    checkHealth('/health/learning_engine'),
  ]);

  const toStatus = (raw: string | undefined): ServiceHealthStatus => {
    if (raw === 'ok') return 'ok';
    if (raw === 'unknown') return 'unknown';
    if (raw === 'unavailable') return 'down';
    return 'unknown';
  };

  const services: ServiceHealth[] = [
    { name: 'paper_ingestion', label: 'Paper Ingestion', status: piOk ? 'ok' : 'down' },
    { name: 'learning_engine', label: 'Learning Engine', status: leOk ? 'ok' : 'down' },
    { name: 'postgres', label: 'PostgreSQL', status: toStatus(depChecks['postgres']) },
    { name: 'qdrant', label: 'Qdrant', status: toStatus(depChecks['qdrant']) },
    { name: 'ollama', label: 'Ollama', status: toStatus(depChecks['ollama']) },
    { name: 'litellm', label: 'LiteLLM', status: toStatus(depChecks['litellm']) },
    { name: 'vector', label: 'Vector', status: toStatus(depChecks['vector']) },
  ];

  const degradedCount = services.filter((s) => s.status === 'degraded').length;
  const downCount = services.filter((s) => s.status === 'down').length;
  const overall: ServiceHealthStatus =
    downCount > 0 ? 'down' : degradedCount > 0 ? 'degraded' : 'ok';

  return { services, degradedCount, downCount, overall };
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
  SourceHealth,
  SourceRunRecord,
  WeeklyDigestResponse,
  YesterdaySummary,
  Thread,
  ThreadSeedResponse,
  AccountResponse,
  AccountUpdateResponse,
  AnalyticsSummaryResponse,
  ProjectQuestion,
  ProjectActivityItem,
  FeedCountsWithFacets,
} from '@/types';

export type { SourceHealth, SourceRunRecord };

// --- Auth (Phase 2 WS-2A magic-link) ---
import type { SessionUser } from '@/stores/auth-store';

/** Request a one-shot magic-link email. Always resolves true regardless of
 *  whether the email exists (the backend deliberately doesn't leak account
 *  existence). Throws ApiError only on network/transport failure. */
export const requestMagicLink = (email: string) =>
  apiFetch<{ sent: boolean }>('/api/auth/request-link', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });

/** Exchange a magic-link token for a session cookie + user record. */
export const verifyMagicLink = (token: string) =>
  apiFetch<SessionUser>('/api/auth/verify', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

/** Revoke the current session and clear the cookie. */
export const logoutSession = () =>
  apiFetch<void>('/api/auth/logout', { method: 'POST' });

// --- Admin user management (Phase 2 WS-2B) ---

export interface AdminUser {
  id: number;
  email: string;
  role: 'user' | 'admin';
  created_at: string;
  last_login_at: string | null;
}

/** List all non-deleted users. Requires admin role. */
export const listUsers = () =>
  apiFetch<AdminUser[]>('/api/admin/users');

/** Invite a new user. Sends them a 24-hour magic link. Requires admin role. */
export const inviteUser = (email: string, role: 'user' | 'admin') =>
  apiFetch<AdminUser>('/api/admin/users', {
    method: 'POST',
    body: JSON.stringify({ email, role }),
  });

/** Change a user's role. Requires admin role. */
export const updateUserRole = (userId: number, role: 'user' | 'admin') =>
  apiFetch<AdminUser>(`/api/admin/users/${userId}/role`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });

/** Soft-delete a user (sets deleted_at). Requires admin role. */
export const deleteUser = (userId: number) =>
  apiFetch<void>(`/api/admin/users/${userId}`, { method: 'DELETE' });

// --- Admin audit log (WS-ADMIN-AUDIT) ---

export interface AuditLogEntry {
  id: number;
  user_id: string | null;
  action: string;
  resource: string;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditLogPage {
  entries: AuditLogEntry[];
  next_before_id: number | null;
}

/** Read the audit log (cursor-paginated, newest first). Requires admin role. */
export const listAuditLog = (params?: {
  limit?: number;
  beforeId?: number | null;
  actionPrefix?: string;
}) => {
  const qs = new URLSearchParams();
  if (params?.limit != null) qs.set('limit', String(params.limit));
  if (params?.beforeId != null) qs.set('before_id', String(params.beforeId));
  if (params?.actionPrefix) qs.set('action_prefix', params.actionPrefix);
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return apiFetch<AuditLogPage>(`/api/admin/audit-log${suffix}`);
};

// --- System readiness (WS-PRE-PUBLIC-CHECKLIST) ---

export interface ReadinessCheck {
  name: string;
  status: 'green' | 'amber' | 'red';
  detail: string;
}

export interface ReadinessResponse {
  status: 'green' | 'amber' | 'red';
  checks: ReadinessCheck[];
}

/** Read overall system readiness. Requires admin role. */
export const getSystemReadiness = () =>
  apiFetch<ReadinessResponse>('/api/system/readiness');

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
export const fetchMySubscriptions = () =>
  apiFetch<number[]>('/api/topics/subscriptions');
export const subscribeToTopic = (topicId: number) =>
  apiFetch<void>(`/api/topics/${topicId}/subscribe`, { method: 'PUT' });
export const unsubscribeFromTopic = (topicId: number) =>
  apiFetch<void>(`/api/topics/${topicId}/subscribe`, { method: 'DELETE' });

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

// --- WS-2F first-run wizard (pre-auth bootstrap) ---
// These call /api/setup/* which is unauthenticated until the first admin exists.
// Distinct surface from /api/system/setup-status above (post-login bootstrap).
export interface FirstRunStatus { configured: boolean }
export interface FirstRunServiceStatus { name: string; ok: boolean; detail: string | null }
export interface FirstRunSystemCheck { services: FirstRunServiceStatus[]; all_ok: boolean }
export interface FirstRunSmtpBody {
  host: string;
  port: number;
  user?: string | null;
  pass?: string | null;
  from_email: string;
  test_send?: boolean;
  test_recipient?: string | null;
}
export interface FirstRunSmtpResponse {
  saved: boolean;
  test_sent: boolean | null;
  test_error: string | null;
}
export interface FirstRunAdminResponse { id: number; email: string; role: string }
export interface FirstRunCloudKeysBody {
  openai?: string | null;
  anthropic?: string | null;
  gemini?: string | null;
}
export interface FirstRunCloudKeysResponse { saved_providers: string[] }

export const getFirstRunStatus = () =>
  apiFetch<FirstRunStatus>('/api/setup/status');

export const runFirstRunSystemCheck = () =>
  apiFetch<FirstRunSystemCheck>('/api/setup/system-check', { method: 'POST' });

export const saveFirstRunSmtp = (body: FirstRunSmtpBody) =>
  apiFetch<FirstRunSmtpResponse>('/api/setup/smtp', {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const createFirstRunAdmin = (email: string) =>
  apiFetch<FirstRunAdminResponse>('/api/setup/admin', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });

export const saveFirstRunCloudKeys = (body: FirstRunCloudKeysBody) =>
  apiFetch<FirstRunCloudKeysResponse>('/api/setup/cloud-llm-keys', {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const createPairingCode = () =>
  apiFetch<TelegramPairing>('/api/telegram/pairing', { method: 'POST' });

export const getPairingStatus = () =>
  apiFetch<TelegramPairingStatus>('/api/telegram/pairing/status');

export const unpairTelegram = () =>
  apiFetch<void>('/api/config/telegram.owner_chat_id', {
    method: 'PUT',
    body: JSON.stringify({ key: 'telegram.owner_chat_id', value: null }),
  });

// --- Per-user multi-tenant Telegram pairing (Sprint A) ---

export interface TelegramPairTokenResponse {
  token: string;
  expires_at: string;
}

export interface UserTelegramPairingStatus {
  paired: boolean;
  chat_id: number | null;
  telegram_username: string | null;
  paired_at: string | null;
}

/** Issue a 15-minute per-user pairing token. Requires an authenticated session. */
export const requestTelegramPairToken = () =>
  apiFetch<TelegramPairTokenResponse>('/api/telegram/pair-token', { method: 'POST' });

/** Return the current user's Telegram pairing status from telegram_user_pairings. */
export const getTelegramPairing = () =>
  apiFetch<UserTelegramPairingStatus>('/api/telegram/pairing');

/** Remove the current user's Telegram pairing. */
export const removeTelegramPairing = () =>
  apiFetch<void>('/api/telegram/pairing', { method: 'DELETE' });

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
/**
 * Analytics "Reflect" KPI band — current/prior-period totals + streaks.
 * GET /api/analytics/summary?days=N (learning_engine analytics router).
 */
export const fetchAnalyticsSummary = (days?: number) =>
  apiFetch<AnalyticsSummaryResponse>(
    `/api/analytics/summary${days ? `?days=${days}` : ''}`,
  );

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

// --- Project Open Questions (UI_v3 Projects § OPEN QUESTIONS) ---
export const fetchProjectQuestions = (projectId: number) =>
  apiFetch<ProjectQuestion[]>(`/api/projects/${projectId}/questions`);
export const createProjectQuestion = (projectId: number, body: string) =>
  apiFetch<ProjectQuestion>(`/api/projects/${projectId}/questions`, {
    method: 'POST',
    body: JSON.stringify({ body }),
  });
/** DELETE is addressed by question id (own /api/questions prefix). */
export const deleteProjectQuestion = (questionId: number) =>
  apiFetch<void>(`/api/questions/${questionId}`, { method: 'DELETE' });

// --- Project Recent Activity (UI_v3 Projects § RECENT ACTIVITY) ---
export const fetchProjectActivity = (projectId: number, limit?: number) =>
  apiFetch<ProjectActivityItem[]>(
    `/api/projects/${projectId}/activity${limit ? `?limit=${limit}` : ''}`,
  );

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
  const filename = (match && match[1]) ? match[1] : `deck_${deckId}.apkg`;
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

/**
 * UI_v3 Feed facet rail — same `GET /api/papers/feed/counts` payload, typed
 * with the additive `by_source` / `by_topic` / `untagged` facet fields the
 * backend always emits (models/papers.py:646-649). The Feed surface agent
 * consumes this for the §-facet rail; `fetchFeedCounts` stays numeric-only so
 * existing `keyof`-indexing consumers (CountsBadge) are untouched.
 */
export async function fetchFeedCountsWithFacets(): Promise<FeedCountsWithFacets> {
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
  scope?: import('@/types').FeedScope;
  limit?: number;
  offset?: number;
  sourceTypes?: string | null;
}): Promise<FeedResponse> {
  const { view, filter, scope, limit = 30, offset = 0, sourceTypes } = params;

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

export const fetchIntentToday = () =>
  apiFetch<{ intent: string | null; updated_at: string | null }>(
    '/api/executive/intent/today',
  );

export const saveIntentToday = (intent: string) =>
  apiFetch<{ intent: string | null; updated_at: string | null }>(
    '/api/executive/intent/today',
    { method: 'POST', body: JSON.stringify({ intent }) },
  );

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

export const getPulseSourceHealth = () =>
  apiFetch<SourceHealth[]>('/api/pulse/source-health');

export const getPulseSourceHistory = (days = 7) =>
  apiFetch<Record<string, SourceRunRecord[]>>(`/api/pulse/source-history?days=${days}`);

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
    credentials: 'include',
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

export async function getJournalEntry(
  date: string,
  options?: { signal?: AbortSignal },
): Promise<JournalEntry | null> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 300_000);
  // Combine the internal timeout signal with any caller-provided signal
  // (e.g. TanStack Query's abort-on-unmount signal).
  const callerSignal = options?.signal;
  const signal = callerSignal
    ? AbortSignal.any([controller.signal, callerSignal])
    : controller.signal;
  try {
    const res = await fetch(`/api/my-day/journal?date=${date}`, {
      signal,
      credentials: 'include',
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

export async function upsertJournalEntry(
  date: string,
  prompts: JournalPrompts,
  signal?: AbortSignal,
): Promise<JournalEntry> {
  return apiFetch<JournalEntry>('/api/my-day/journal', {
    method: 'POST',
    body: JSON.stringify({ date, prompts }),
    signal,
  });
}

// --- My Day § Yesterday (UI_v3, on-the-fly rollup) ---

/**
 * GET /api/my-day/yesterday — on-the-fly § Yesterday rollup.
 * `tzOffsetMinutes` = minutes EAST of UTC (JS `-new Date().getTimezoneOffset()`);
 * the server stores no per-user timezone so the client supplies it.
 */
export const fetchYesterday = (tzOffsetMinutes = 0) =>
  apiFetch<YesterdaySummary>(
    `/api/my-day/yesterday?tz_offset_minutes=${tzOffsetMinutes}`,
  );

// --- My Day § Open threads (UI_v3 `thread` entity) ---

export const fetchThreads = () =>
  apiFetch<Thread[]>('/api/my-day/threads');
export const fetchThread = (threadId: number) =>
  apiFetch<Thread>(`/api/my-day/threads/${threadId}`);
export const createThread = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}) =>
  apiFetch<Thread>('/api/my-day/threads', {
    method: 'POST',
    body: JSON.stringify(data),
  });
export const updateThread = (
  threadId: number,
  data: { title?: string; anchor?: string | null; progress?: number; status?: string },
) =>
  apiFetch<Thread>(`/api/my-day/threads/${threadId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
/** The prototype's `resume →` action — bumps last_at and returns the thread. */
export const resumeThread = (threadId: number) =>
  apiFetch<Thread>(`/api/my-day/threads/${threadId}/resume`, { method: 'POST' });
/** Auto-seed producer 1 — interrupted Pomodoro session → thread. */
export const seedThreadFromPomodoro = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}) =>
  apiFetch<ThreadSeedResponse>('/api/my-day/threads/seed/pomodoro', {
    method: 'POST',
    body: JSON.stringify(data),
  });
/** Auto-seed producer 2 — EOD "make this a thread" → thread. */
export const seedThreadFromEod = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}) =>
  apiFetch<ThreadSeedResponse>('/api/my-day/threads/seed/eod', {
    method: 'POST',
    body: JSON.stringify(data),
  });

// --- §I Account (UI_v3 self-service profile) ---

export const fetchAccount = () => apiFetch<AccountResponse>('/api/account');
/**
 * PATCH /api/account — `display_name` applies immediately; an `email` change
 * is never silent (issues a verification link to the new address).
 */
export const updateAccount = (data: { display_name?: string | null; email?: string }) =>
  apiFetch<AccountUpdateResponse>('/api/account', {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
/** Consume the email-change token (mirrors /api/auth/verify). */
export const confirmEmailChange = (token: string) =>
  apiFetch<AccountResponse>('/api/account/confirm-email', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

// --- Weekly Digest ---

export async function fetchWeeklyDigest(days: number = 7): Promise<WeeklyDigestResponse> {
  return apiFetch<WeeklyDigestResponse>(`/api/digest/weekly?days=${days}`);
}

// --- React Query hooks ---
export { useFeedCounts } from '@/hooks/use-feed-counts';

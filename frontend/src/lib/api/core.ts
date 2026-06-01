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
 *
 * This module holds the shared primitives every domain submodule depends on:
 * `apiFetch`, `apiFetchRaw`, `ApiError`, the auth-header helper, the
 * auto-logout handler (+ its debounce singleton), the blob-download helper,
 * and the stack-health helpers/types. Domain submodules import from HERE,
 * never from the barrel `./index`, to keep the index↔domain graph acyclic.
 */

import { toast } from 'sonner';
import { useAuthStore } from '@/stores/auth-store';

/** Build auth headers from the current session API key. */
export function authHeaders(): Record<string, string> {
  const apiKey = useAuthStore.getState().getApiKey();
  return apiKey ? { 'X-API-Key': apiKey } : {};
}

export let _sessionExpiredToastShownAt = 0;

/** Auto-logout on genuine auth failure (401 only). */
export function handleAuthFailure(status: number): void {
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
      // Send the jarvis_session HttpOnly cookie on every API call so
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
      // Same rationale as apiFetch — carry the jarvis_session cookie.
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
    if (raw === 'degraded') return 'degraded';
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

/** Trigger a browser download for a blob (shared by Anki/CSV exporters). */
export function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

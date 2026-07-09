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
import { useMaintenanceStore } from '@/stores/maintenance-store';

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

/**
 * Shared fetch core for apiFetch and apiFetchRaw.
 *
 * Owns the 5-min timeout controller, caller-signal combination, cookie
 * credentials, auth headers, the !res.ok error path (auto-logout + ApiError +
 * maintenance-mode detection on a machine-readable 503), and the abort/error
 * translation. Returns the raw ok Response; callers decide whether to parse
 * JSON. The Content-Type default is supplied by the caller via `init.headers`
 * so blob/raw callers can omit it.
 */
async function _doFetch(url: string, init?: RequestInit): Promise<Response> {
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
        ...authHeaders(),
        ...init?.headers,
      },
    });
    if (!res.ok) {
      const bodyText = await res.text();
      if (res.status === 503) {
        try {
          const parsed = JSON.parse(bodyText);
          if (parsed?.detail === 'Restore in progress') {
            useMaintenanceStore
              .getState()
              .setMaintenance(true, Number(res.headers.get('retry-after')) || 30);
          }
        } catch {
          // non-JSON 503 body — not a maintenance signal
        }
      }
      handleAuthFailure(res.status);
      throw new ApiError(res.status, bodyText);
    }
    return res;
  } catch (err) {
    _handleFetchError(err, controller, init?.signal);
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await _doFetch(url, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  });
  if (res.status === 204) {
    return undefined as T;
  }
  return res.json();
}

/** Fetch that returns the raw Response (for blob downloads). */
export async function apiFetchRaw(url: string, init?: RequestInit): Promise<Response> {
  return _doFetch(url, init);
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

/**
 * Overall stack rollup. 'maintenance' (a restore is running) exists only at
 * the rollup level — per-service statuses stay plain ServiceHealthStatus.
 */
export type StackOverall = ServiceHealthStatus | 'maintenance';

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
  /** Overall rollup: maintenance takes precedence over down / degraded / unknown / ok */
  overall: StackOverall;
  /** True while a restore holds the maintenance sentinel (from /health/internal). */
  maintenance?: boolean;
  /** Backend application version (from /health/internal). */
  version?: string;
}

/** Static labels for every stack component, in display order. */
const STACK_HEALTH_SERVICES: ReadonlyArray<{ name: string; label: string }> = [
  { name: 'paper_ingestion', label: 'Paper Ingestion' },
  { name: 'learning_engine', label: 'Learning Engine' },
  { name: 'postgres', label: 'PostgreSQL' },
  { name: 'qdrant', label: 'Qdrant' },
  { name: 'ollama', label: 'Ollama' },
  { name: 'litellm', label: 'LiteLLM' },
  { name: 'vector', label: 'Vector' },
];

/** Hard deadline for {@link fetchStackHealth} so the UI never stays "Checking…". */
const STACK_HEALTH_DEADLINE_MS = 5000;

/**
 * Timeout/no-response fallback: every service is reported as 'unknown' so
 * callers leave the "checking" state and render neutral dots rather than
 * hanging forever. A real 'down' from a responding endpoint always returns
 * its true per-service statuses via the normal path.
 */
function unknownStackHealth(): StackHealthSummary {
  const services: ServiceHealth[] = STACK_HEALTH_SERVICES.map((s) => ({
    name: s.name,
    label: s.label,
    status: 'unknown',
  }));
  return {
    services,
    degradedCount: 0,
    downCount: 0,
    overall: 'unknown',
    // A probe timeout does not know the maintenance state — undefined, not
    // false, so a timeout mid-restore never satisfies the banner's clear check.
    maintenance: undefined,
  };
}

/**
 * Probe every stack component and assemble the real per-service summary.
 *
 * Calls liveness endpoints for process-level service rows (paper_ingestion,
 * learning_engine) and the authenticated internal endpoint for dependency
 * readiness breakdown (postgres, qdrant, ollama, litellm, vector).
 *
 * Individual fetch failures are mapped to 'down' so a single unreachable
 * service never throws — callers always get a StackHealthSummary.
 */
async function probeStackHealth(): Promise<StackHealthSummary> {
  // Dependency statuses from paper_ingestion internal health endpoint
  const depLabels: Record<string, string> = {
    postgres: 'PostgreSQL',
    qdrant: 'Qdrant',
    litellm: 'LiteLLM',
    ollama: 'Ollama',
    vector: 'Vector',
  };

  // Fire all three probes concurrently so wall time is max(one probe), not the
  // sum — comfortably under STACK_HEALTH_DEADLINE_MS. allSettled so one rejected
  // probe never drops the others' results.
  const [internal, piOk, leOk] = await Promise.all([
    apiFetch<{
      status: string;
      checks: Record<string, string>;
      maintenance?: boolean;
      version?: string;
    }>('/health/paper_ingestion/internal').then(
      (payload) => ({
        checks: payload.checks ?? {},
        maintenance: payload.maintenance,
        version: payload.version,
      }),
      // A 503 "degraded" internal-health response is informative, not
      // unreachable: during a restore's DB reload the postgres probe is down
      // (degraded → 503) yet the body still reports maintenance:true. Recover
      // checks/maintenance/version from a degraded ApiError body so the rollup
      // reflects 'maintenance' (not 'unknown') and the banner does not wrongly
      // clear. Only a true transport failure / non-JSON body falls to all-unknown.
      (err: unknown) => {
        if (err instanceof ApiError && err.status === 503) {
          try {
            const parsed = JSON.parse(err.body) as {
              checks?: Record<string, string>;
              maintenance?: boolean;
              version?: string;
            };
            return {
              checks: parsed.checks ?? {},
              maintenance: parsed.maintenance,
              version: parsed.version,
            };
          } catch {
            // non-JSON 503 body → fall through to all-unknown
          }
        }
        return {
          checks: Object.fromEntries(Object.keys(depLabels).map((key) => [key, 'unknown'])),
          maintenance: undefined,
          version: undefined,
        };
      },
    ),
    checkHealth('/health/paper_ingestion/live'),
    checkHealth('/health/learning_engine/live'),
  ]);
  const depChecks: Record<string, string> = internal.checks;

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
  // 'vector' (the log collector) is optional: an unknown vector never degrades
  // the rollup. Any *non-optional* service that is 'unknown' keeps the overall
  // off 'ok' — we never report "All healthy" while a required dep is unverified.
  const requiredUnknown = services.some(
    (s) => s.name !== 'vector' && s.status === 'unknown',
  );
  // A live restore (maintenance sentinel) trumps every per-service status:
  // the stack is intentionally offline, not broken.
  const overall: StackOverall =
    internal.maintenance === true
      ? 'maintenance'
      : downCount > 0
        ? 'down'
        : degradedCount > 0
          ? 'degraded'
          : requiredUnknown
            ? 'unknown'
            : 'ok';

  return {
    services,
    degradedCount,
    downCount,
    overall,
    maintenance: internal.maintenance,
    version: internal.version,
  };
}

/**
 * Fetch full health status for all stack components, with a hard deadline.
 *
 * The underlying probes (apiFetch / checkHealth) share the 5-min request
 * timeout, so a network black-hole could otherwise leave the health UI stuck
 * on "Checking…" for minutes. We race the real probe against a
 * {@link STACK_HEALTH_DEADLINE_MS} timer: if the probe doesn't settle in time,
 * we resolve to a synthesized all-'unknown' degraded summary so callers always
 * leave the checking state quickly. The function always settles (never rejects);
 * the real success/failure path is unchanged.
 */
export async function fetchStackHealth(): Promise<StackHealthSummary> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<StackHealthSummary>((resolve) => {
    timeoutId = setTimeout(() => resolve(unknownStackHealth()), STACK_HEALTH_DEADLINE_MS);
  });
  try {
    return await Promise.race([probeStackHealth(), deadline]);
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
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

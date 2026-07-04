// System readiness, model catalog, capabilities, dashboard metrics, and
// AI-backend settings.
import { apiFetch } from './core';
import type {
  DashboardMetrics,
  SystemCapabilities,
} from '@/types';

// --- System readiness ---

export interface ReadinessCheck {
  name: string;
  status: 'green' | 'amber' | 'red';
  detail: string;
  remediation?: string;
}

export interface ReadinessResponse {
  status: 'green' | 'amber' | 'red';
  checks: ReadinessCheck[];
}

/** Read overall system readiness. Requires admin role. */
export const getSystemReadiness = () =>
  apiFetch<ReadinessResponse>('/api/system/readiness');

// --- System models (GET /api/system/models) ---
/** Per-alias recommendation entry returned by GET /api/system/models hardware_recommendation. */
export interface HardwareRecommendationAlias {
  alias: 'smart' | 'fast' | 'embed';
  model: string;
  confirm_on_target: boolean;
  notes: string;
}

/**
 * Hardware-fit advisory returned by GET /api/system/models.
 * Optional — older backends omit this field; UI degrades gracefully.
 */
export interface HardwareRecommendation {
  vram_mb: number | null;
  bucket: 'CPU_ONLY' | 'ENTRY' | 'MID' | 'MID_HIGH' | 'HIGH';
  summary: string;
  aliases: HardwareRecommendationAlias[];
}

// Canonical response shape for /api/system/models.
// IngestionSection uses hardware/catalog/hardware_recommendation;
// ModelSelector uses status/installed/current/issues/catalog/hardware.
// Both call the same endpoint — this interface is the structural union.
export interface SystemModelsResponse {
  status?: 'ok' | 'degraded';
  installed?: unknown[];
  hardware?: {
    vram_gb?: number;
    vram_source?: string;
    tier?: number;
    detected_at?: string;
    ollama_running?: number;
    machine_id?: string;
  };
  current?: Record<string, string>;
  issues?: Record<string, string>;
  catalog?: unknown[];
  recommendations?: Record<string, unknown>;
  /** Advisory per-VRAM model recommendation. Optional — absent on older backends. */
  hardware_recommendation?: HardwareRecommendation;
  /**
   * Per-role model LiteLLM is currently routing, normalized (provider prefix stripped for
   * ollama/ models so it compares directly to `current` values). Absent when LiteLLM is
   * unreachable. Additive — absent on older backends.
   */
  routing?: Record<string, string>;
  /**
   * True when every role with a stored `current` intent is also reflected in `routing`.
   * False when LiteLLM is unreachable and there is stored intent that cannot be verified.
   * Defaults to true when absent (older backends without T1.3).
   */
  consistent?: boolean;
}

/**
 * Fetch model catalog and hardware info. Pass TanStack Query's `signal` for abort-on-unmount.
 *
 * Generic so callers can narrow to a local interface (e.g. `fetchSystemModels<SystemModelsApi>`).
 * The default `T` is `SystemModelsResponse` — the canonical structural union of all known shapes.
 * `T extends Partial<SystemModelsResponse>` ensures callers only narrow to structurally compatible
 * subsets; any call-site type with a property absent from `SystemModelsResponse` is a type error.
 */
export async function fetchSystemModels<T extends Partial<SystemModelsResponse> = SystemModelsResponse>(signal?: AbortSignal): Promise<T> {
  return apiFetch<T>('/api/system/models', { signal });
}

// --- Dashboard ---
export const fetchDashboardMetrics = () =>
  apiFetch<DashboardMetrics>('/api/dashboard/metrics');

// --- System capabilities ---

export const getSystemCapabilities = () =>
  apiFetch<SystemCapabilities>('/api/system/capabilities');

// --- Settings: AI backend ---

export interface AIBackendCandidate {
  backend: 'ollama' | 'vllm';
  model: string;
  catalog_id?: string | null;
  source?: 'catalog' | 'tier-candidates';
  rank: number;
  score?: number | null;
  reasoning?: string;
  evidence?: 'bench' | 'sim-bench' | 'static-benchmark' | 'pending-bench' | 'catalog' | null;
}

export interface AISettings {
  hw_tier: string;
  recommended_backend: string;
  recommended_model: string;
  configured_backend: string | null;
  configured_model: string | null;
  observed_backend: string | null;
  observed_recent_share: number;
  candidates_for_tier: AIBackendCandidate[];
  candidate_issues: string[];
  eval_report_date: string | null;
}

export function getAISettings() {
  return apiFetch<AISettings>('/api/settings/ai');
}

export function postAISettings(body: { backend: string; model: string }) {
  return apiFetch<AISettings>('/api/settings/ai', { method: 'POST', body: JSON.stringify(body) });
}

export function redetectHW() {
  return apiFetch<AISettings>('/api/settings/ai/redetect', { method: 'POST' });
}

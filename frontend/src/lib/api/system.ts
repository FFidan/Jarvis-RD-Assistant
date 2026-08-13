// System readiness, model catalog, capabilities, dashboard metrics, and
// AI-backend settings.
import { apiFetchJson } from './core';
import {
  aiSettingsSchema,
  dashboardMetricsSchema,
  readinessResponseSchema,
  systemCapabilitiesSchema,
  systemModelsResponseSchema,
  systemStorageResponseSchema,
} from './schemas/system';
export type {
  AIBackendCandidate,
  AISettings,
  HardwareRecommendation,
  HardwareRecommendationAlias,
  ModelCatalogEntry,
  ProviderModelListStatus,
  QdrantCollectionUsage,
  ReadinessCheck,
  ReadinessResponse,
  StorageSection,
  SystemModelsResponse,
  SystemStorageResponse,
} from './schemas/system';

// --- System readiness ---

/** Read overall system readiness. Requires admin role. */
export const getSystemReadiness = () =>
  apiFetchJson('/api/system/readiness', readinessResponseSchema);

// --- System storage (GET /api/system/storage) ---

/**
 * One storage backend's usage. `bytes_used` is null when the size is
 * unknown: either the backend was unreachable (`error` set) or it has no
 * byte-level size API (Qdrant — see `qdrant_collections` for its proxy).
 */
/** Disk-usage snapshot across backing stores. Requires admin role (or API key). */
export const getSystemStorage = () =>
  apiFetchJson('/api/system/storage', systemStorageResponseSchema);

// --- System models (GET /api/system/models) ---
/**
 * Fetch model catalog and hardware info. Pass TanStack Query's `signal` for abort-on-unmount.
 */
export async function fetchSystemModels(signal?: AbortSignal) {
  return apiFetchJson('/api/system/models', systemModelsResponseSchema, { signal });
}

// --- Dashboard ---
export const fetchDashboardMetrics = () =>
  apiFetchJson('/api/dashboard/metrics', dashboardMetricsSchema);

// --- System capabilities ---

export const getSystemCapabilities = () =>
  apiFetchJson('/api/system/capabilities', systemCapabilitiesSchema);

// --- Settings: AI backend ---

export function getAISettings() {
  return apiFetchJson('/api/settings/ai', aiSettingsSchema);
}

export function redetectHW() {
  return apiFetchJson('/api/settings/ai/redetect', aiSettingsSchema, { method: 'POST' });
}

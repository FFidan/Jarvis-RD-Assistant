/**
 * API client barrel.
 *
 * Re-exports the full public surface that the former `src/lib/api.ts`
 * god-module exposed, so every `@/lib/api` import (incl. the ~91 test mocks)
 * resolves identically. The implementation now lives in domain submodules
 * (`./auth`, `./system`, `./settings`, …) that all import their shared
 * primitives from `./core` — never from this barrel — keeping the module
 * graph acyclic.
 *
 * `./core` is re-exported EXPLICITLY (not `export *`) so its internal helpers
 * — `authHeaders`, `handleAuthFailure`, `_sessionExpiredToastShownAt`,
 * `triggerBlobDownload` — stay out of the public surface, exactly as they were
 * un-exported from the original `api.ts`.
 */

// --- Shared primitives (core) — explicit so internal helpers stay private ---
export { ApiError, apiFetch, apiFetchRaw, checkHealth, fetchStackHealth } from './core';
export type { ServiceHealth, ServiceHealthStatus, StackHealthSummary } from './core';

// Types re-exported by the original api.ts from '@/types'.
export type { SourceHealth, SourceRunRecord } from '@/types';

// --- Domain modules (every export here was public on the original api.ts) ---
export * from './auth';
export * from './system';
export * from './settings';
export * from './analytics';
export * from './projects';
export * from './cards';
export * from './papers';
export * from './highlights';
export * from './pulse';
export * from './jobs';
export * from './zotero';
export * from './myday';
export * from './backups';

// Admin Backup panel client — cookie-session admin auth (no X-API-Key needed,
// but apiFetch sends the key harmlessly). Mirrors the system.ts client shape.
import { apiFetch, apiFetchRaw, triggerBlobDownload } from './core';

export interface BackupEntry {
  filename: string;
  store: 'jarvis' | 'litellm' | 'secrets' | 'qdrant';
  size_bytes: number;
  modified_at: string;
  encrypted: boolean;
}

export interface BackupStatus {
  backup_dir_available: boolean;
  archive_count: number;
  last_run_at: string | null;
  trigger_pending: boolean;
  last_attempt_at: string | null;
  last_run_succeeded: boolean | null;
}

export interface RestorePointFile {
  filename: string;
  store: BackupEntry['store'];
  size_bytes: number;
  encrypted: boolean;
}

export interface RestorePoint {
  timestamp: string;
  created_at: string;
  stores: BackupEntry['store'][];
  qdrant_collections: string[];
  complete: boolean;
  encrypted: boolean;
  total_size_bytes: number;
  files: RestorePointFile[];
  app_version: string | null;
  schema_version: number | null;
  compat: 'same' | 'older' | 'newer' | 'unknown';
}

export type RestoreSource = 'local' | 'inbox';

export interface RestoreRequest {
  timestamp: string;
  confirm: string;
  source: RestoreSource;
}

/** One off-host restore point staged in the restore_inbox (sidecar-authored manifest). */
export interface InboxRestorePoint {
  timestamp: string;
  complete: boolean;
  has_secrets: boolean;
  has_key: boolean;
}

export interface RestoreStatus {
  state: string;
  current_step: string | null;
  steps: { name: string; status: string }[];
  safety_backup_ts: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  manual_steps_required: boolean;
  phase: string | null;
}

export interface RestoreLastRun {
  attempted_at: string | null;
  succeeded: boolean | null;
  stores: Record<string, string>;
}

export interface RestorePointsResponse {
  restore_points: RestorePoint[];
  retention_days: number | null;
  last_run: RestoreLastRun | null;
}

export interface RetentionConfig {
  keep_last_n: number | null;
  max_age_days: number | null;
}

/** Sidecar reachability + inferred last-run time. */
export const getBackupStatus = () =>
  apiFetch<BackupStatus>('/api/admin/backups/status');

/** Group archives into restore points (one per backup run). Requires admin session. */
export const getRestorePoints = () =>
  apiFetch<RestorePointsResponse>('/api/admin/backups/restore-points');

/** Request an on-demand backup (writes a sentinel the sidecar polls). */
export const triggerBackup = () =>
  apiFetch<{ status: string }>('/api/admin/backups', { method: 'POST' });

/** Stream a single archive to the browser as a download. */
export async function downloadBackup(name: string): Promise<void> {
  const res = await apiFetchRaw(
    `/api/admin/backups/${encodeURIComponent(name)}/download`,
  );
  triggerBlobDownload(await res.blob(), name);
}

/**
 * Start a one-click restore from the named restore point. `confirm` gates the
 * destructive op; `source` selects the local /backups set (default) or the off-host
 * inbox. Returns the one-time status bearer token so the progress poll can survive
 * the restore tearing down the admin session (pass it to {@link getRestoreStatus}).
 */
export const requestRestore = (
  timestamp: string,
  confirm: string,
  source: RestoreSource = 'local',
) =>
  apiFetch<{ status: string; status_token?: string }>('/api/admin/backups/restore', {
    method: 'POST',
    body: JSON.stringify({ timestamp, confirm, source } satisfies RestoreRequest),
  });

/**
 * Poll the live restore progress (state machine + per-step status). When a one-time
 * bearer `token` is supplied it authorizes the poll DB-free, so it keeps returning
 * progress through the DB swap that drops the admin's session; without a token it
 * falls back to the cookie/API-key path (which dies mid-swap).
 */
export const getRestoreStatus = (token?: string) =>
  apiFetch<RestoreStatus>(
    '/api/admin/backups/restore/status',
    token ? { headers: { Authorization: `Bearer ${token}` } } : undefined,
  );

/** List off-host restore points staged in the restore_inbox (sidecar-authored). */
export const getInboxRestorePoints = () =>
  apiFetch<InboxRestorePoint[]>('/api/admin/backups/inbox');

/**
 * Request deletion of a restore point. `confirm` ('DELETE') gates the destructive
 * op. Writes a sentinel the sidecar's prune executes — the app deletes nothing.
 */
export const deleteRestorePoint = (timestamp: string, confirm: string) =>
  apiFetch<{ status: string }>(
    `/api/admin/backups/restore-points/${encodeURIComponent(timestamp)}/delete`,
    { method: 'POST', body: JSON.stringify({ confirm }) },
  );

/** Read the backup retention policy (keep-last-N + max-age-days; nulls = env default). */
export const getRetention = () => apiFetch<RetentionConfig>('/api/admin/backups/retention');

/** Update the backup retention policy the sidecar reads. */
export const putRetention = (config: RetentionConfig) =>
  apiFetch<RetentionConfig>('/api/admin/backups/retention', {
    method: 'PUT',
    body: JSON.stringify(config),
  });

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

export interface RestoreRequest {
  timestamp: string;
  confirm: string;
}

export interface RestoreStatus {
  state: string;
  current_step: string | null;
  steps: { name: string; status: string }[];
  safety_backup_ts: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
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

/** Start a one-click restore from the named restore point. `confirm` gates the destructive op. */
export const requestRestore = (timestamp: string, confirm: string) =>
  apiFetch<{ status: string }>('/api/admin/backups/restore', {
    method: 'POST',
    body: JSON.stringify({ timestamp, confirm } satisfies RestoreRequest),
  });

/** Poll the live restore progress (state machine + per-step status). */
export const getRestoreStatus = () =>
  apiFetch<RestoreStatus>('/api/admin/backups/restore/status');

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
}

/** List backup archive metadata, newest first. Requires admin session. */
export const listBackups = () => apiFetch<BackupEntry[]>('/api/admin/backups');

/** Sidecar reachability + inferred last-run time. */
export const getBackupStatus = () =>
  apiFetch<BackupStatus>('/api/admin/backups/status');

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

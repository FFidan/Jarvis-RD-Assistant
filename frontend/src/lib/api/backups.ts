// Admin Backup panel client — cookie-session admin auth (no X-API-Key needed,
// but apiFetch sends the key harmlessly). Mirrors the system.ts client shape.
import { apiFetch, apiFetchRaw, triggerBlobDownload } from './core';

export interface BackupEntry {
  filename: string;
  store: 'jarvis' | 'litellm' | 'pdfs' | 'secrets' | 'qdrant';
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
  has_pdfs: boolean;
  legacy_missing_pdfs: boolean;
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
  allow_missing_pdfs: boolean;
  allow_unknown_schema: boolean;
}

/** One off-host restore point staged in the restore_inbox (sidecar-authored manifest). */
export interface InboxRestorePoint {
  timestamp: string;
  complete: boolean;
  has_secrets: boolean;
  has_key: boolean;
  has_pdfs: boolean;
  legacy_missing_pdfs: boolean;
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
  restore_id: string | null;
  source: RestoreSource | null;
  quarantine: 'none' | 'awaiting_review' | 'unreadable';
}

export interface RestoreRequestResponse {
  status: string;
  status_token: string;
  restore_id: string;
  source: RestoreSource;
  expires_at: string;
}

/** Tab-scoped restore session used to resume one exact restore after reload. */
export interface RestoreRecoveryRecord {
  version: 1;
  restore_id: string;
  source: RestoreSource;
  status_token: string;
  expires_at: string;
  target_timestamp: string;
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
 * Start a restore from the named restore point. `confirm` authorizes the
 * destructive operation; `source` selects the local /backups set (default) or the off-host
 * inbox. `allowMissingPdfs` requests the older-backup compatibility path;
 * `allowUnknownSchema` accepts a restore point that records no usable database
 * schema version, which the restore service otherwise refuses because it cannot
 * check compatibility. The restore service rechecks authenticity before changing
 * data. Returns the restore-session token and its exact server expiry so progress
 * polling survives replacement of the admin session (pass the token to
 * {@link getRestoreStatus}).
 */
export const requestRestore = (
  timestamp: string,
  confirm: string,
  source: RestoreSource = 'local',
  allowMissingPdfs = false,
  allowUnknownSchema = false,
) =>
  apiFetch<RestoreRequestResponse>('/api/admin/backups/restore', {
    method: 'POST',
    body: JSON.stringify({
      timestamp,
      confirm,
      source,
      allow_missing_pdfs: allowMissingPdfs,
      allow_unknown_schema: allowUnknownSchema,
    } satisfies RestoreRequest),
  });

/**
 * Poll live restore progress, quarantine state, and per-step status. A supplied
 * restore-session `token` authorizes polling without the database while session
 * rows are replaced; otherwise the request uses its cookie or API key.
 */
export const getRestoreStatus = (token?: string) =>
  apiFetch<RestoreStatus>(
    '/api/admin/backups/restore/status',
    token ? { headers: { Authorization: `Bearer ${token}` } } : undefined,
  );

/** Clear one exact off-host quarantine after the operator reviews restored connections. */
export const acknowledgeRestore = (
  restoreId: string,
  source: 'inbox',
  confirm: string,
  token?: string,
) =>
  apiFetch<{ status: 'acknowledged'; restore_id: string }>(
    '/api/admin/backups/restore/acknowledge',
    {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: JSON.stringify({ restore_id: restoreId, source, confirm }),
    },
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

export interface UploadGrant {
  grant_token: string;
  expires_in_seconds: number;
}

/** Mint the one-time grant (30-min expiry) that authorizes a browser off-host upload. */
export const createUploadGrant = () =>
  apiFetch<UploadGrant>('/api/admin/backups/upload-grant', { method: 'POST' });

/** Operator-facing messages for the restore-uploader's typed denials. */
const UPLOAD_ERROR_DETAILS: Record<number, string> = {
  0: 'Network error during the upload — check the connection and retry.',
  400: 'The server rejected the file (disallowed name or incomplete transfer) — retry the upload.',
  401: 'Upload grant missing — generate an upload grant first.',
  403: 'Upload grant invalid or expired — generate a new grant, then retry.',
  413: 'The file exceeds the server upload size limit.',
  507: 'Not enough free disk space on the server for this file.',
};

export class UploadError extends Error {
  constructor(public status: number) {
    super(UPLOAD_ERROR_DETAILS[status] ?? `Upload failed (HTTP ${status}) — retry the upload.`);
    this.name = 'UploadError';
  }
}

/**
 * Stream one backup file to the restore-uploader sidecar
 * (PUT /restore-upload/<filename>, authorized by the X-Upload-Grant header).
 * XMLHttpRequest instead of fetch solely for upload-progress events; `signal`
 * aborts the transfer. Bypasses apiFetch on purpose: the uploader checks only
 * the grant (no cookies/API key) and the bytes never traverse the app.
 */
export function uploadRestoreFile(
  filename: string,
  file: Blob,
  grantToken: string,
  onProgress?: (percent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/restore-upload/${encodeURIComponent(filename)}`);
    xhr.setRequestHeader('X-Upload-Grant', grantToken);
    const onAbort = () => xhr.abort();
    signal?.addEventListener('abort', onAbort, { once: true });
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => (xhr.status === 201 ? resolve() : reject(new UploadError(xhr.status)));
    xhr.onerror = () => reject(new UploadError(0));
    xhr.onabort = () => reject(new DOMException('The upload was aborted.', 'AbortError'));
    xhr.onloadend = () => signal?.removeEventListener('abort', onAbort);
    xhr.send(file);
  });
}

/** Read the backup retention policy (keep-last-N + max-age-days; nulls = env default). */
export const getRetention = () => apiFetch<RetentionConfig>('/api/admin/backups/retention');

/** Update the backup retention policy the sidecar reads. */
export const putRetention = (config: RetentionConfig) =>
  apiFetch<RetentionConfig>('/api/admin/backups/retention', {
    method: 'PUT',
    body: JSON.stringify(config),
  });

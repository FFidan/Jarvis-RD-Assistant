// Admin Backup panel client — cookie-session admin auth (no X-API-Key needed,
// but the shared client sends the key harmlessly). Mirrors the system.ts client shape.
import { apiFetchJson, apiFetchRaw, triggerBlobDownload } from './core';
import {
  acknowledgedRestoreSchema,
  backupStatusSchema,
  inboxRestorePointSchema,
  restorePointsResponseSchema,
  restoreRequestResponseSchema,
  restoreStatusSchema,
  retentionConfigSchema,
  scheduledResponseSchema,
  uploadGrantSchema,
} from './schemas/backups';
import type {
  BackupEntry,
  BackupStatus,
  InboxRestorePoint,
  RestoreLastRun,
  RestorePoint,
  RestorePointFile,
  RestorePointsResponse,
  RestoreRequestResponse,
  RestoreSource,
  RestoreStatus,
  RetentionConfig,
  UploadGrant,
} from './schemas/backups';

export type {
  BackupEntry,
  BackupStatus,
  InboxRestorePoint,
  RestoreLastRun,
  RestorePoint,
  RestorePointFile,
  RestorePointsResponse,
  RestoreRequestResponse,
  RestoreSource,
  RestoreStatus,
  RetentionConfig,
  UploadGrant,
};

export interface RestoreRequest {
  timestamp: string;
  confirm: string;
  source: RestoreSource;
  allow_missing_pdfs: boolean;
  allow_unknown_schema: boolean;
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

/** Sidecar reachability + inferred last-run time. */
export const getBackupStatus = () =>
  apiFetchJson('/api/admin/backups/status', backupStatusSchema);

/** Group archives into restore points (one per backup run). Requires admin session. */
export const getRestorePoints = () =>
  apiFetchJson('/api/admin/backups/restore-points', restorePointsResponseSchema);

/** Request an on-demand backup (writes a sentinel the sidecar polls). */
export const triggerBackup = () =>
  apiFetchJson('/api/admin/backups', scheduledResponseSchema, { method: 'POST' });

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
  apiFetchJson('/api/admin/backups/restore', restoreRequestResponseSchema, {
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
  apiFetchJson(
    '/api/admin/backups/restore/status',
    restoreStatusSchema,
    token ? { headers: { Authorization: `Bearer ${token}` } } : undefined,
  );

/** Clear one exact off-host quarantine after the operator reviews restored connections. */
export const acknowledgeRestore = (
  restoreId: string,
  source: 'inbox',
  confirm: string,
  token?: string,
) =>
  apiFetchJson(
    '/api/admin/backups/restore/acknowledge',
    acknowledgedRestoreSchema,
    {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: JSON.stringify({ restore_id: restoreId, source, confirm }),
    },
  );

/** List off-host restore points staged in the restore_inbox (sidecar-authored). */
export const getInboxRestorePoints = () =>
  apiFetchJson('/api/admin/backups/inbox', inboxRestorePointSchema.array());

/**
 * Request deletion of a restore point. `confirm` ('DELETE') gates the destructive
 * op. Writes a sentinel the sidecar's prune executes — the app deletes nothing.
 */
export const deleteRestorePoint = (timestamp: string, confirm: string) =>
  apiFetchJson(
    `/api/admin/backups/restore-points/${encodeURIComponent(timestamp)}/delete`,
    scheduledResponseSchema,
    { method: 'POST', body: JSON.stringify({ confirm }) },
  );

/** Mint the one-time grant (30-min expiry) that authorizes a browser off-host upload. */
export const createUploadGrant = () =>
  apiFetchJson('/api/admin/backups/upload-grant', uploadGrantSchema, { method: 'POST' });

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
 * aborts the transfer. Bypasses the shared fetch helper on purpose: the uploader checks only
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
export const getRetention = () =>
  apiFetchJson('/api/admin/backups/retention', retentionConfigSchema);

/** Update the backup retention policy the sidecar reads. */
export const putRetention = (config: RetentionConfig) =>
  apiFetchJson('/api/admin/backups/retention', retentionConfigSchema, {
    method: 'PUT',
    body: JSON.stringify(config),
  });

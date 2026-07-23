import type { RestoreRecoveryRecord } from './api/backups';

export const RESTORE_RECOVERY_STORAGE_KEY = 'jarvis.restore-recovery.v1';

const RESTORE_ID_RE = /^[0-9a-f]{32}$/;
const RESTORE_TIMESTAMP_RE = /^\d{8}_\d{6}$/;
const CAPABILITY_RE = /^[A-Za-z0-9_-]{16,512}$/;
const RECORD_KEYS = [
  'expires_at',
  'restore_id',
  'source',
  'status_token',
  'target_timestamp',
  'version',
] as const;

function isStringKeyedRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Parse one exact, unexpired browser recovery record without logging its token. */
export function parseRestoreRecoveryRecord(
  raw: string | null,
  nowMs = Date.now(),
): RestoreRecoveryRecord | null {
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isStringKeyedRecord(value)) return null;
    const record = value;
    const keys = Object.keys(record).sort();
    if (keys.length !== RECORD_KEYS.length || keys.some((key, index) => key !== RECORD_KEYS[index])) {
      return null;
    }
    const restoreId = record.restore_id;
    const source = record.source;
    const statusToken = record.status_token;
    const expiresAtValue = record.expires_at;
    const targetTimestamp = record.target_timestamp;
    const expiresAt = typeof expiresAtValue === 'string' ? Date.parse(expiresAtValue) : NaN;
    if (
      record.version !== 1 ||
      typeof restoreId !== 'string' ||
      !RESTORE_ID_RE.test(restoreId) ||
      (source !== 'local' && source !== 'inbox') ||
      typeof statusToken !== 'string' ||
      !CAPABILITY_RE.test(statusToken) ||
      !Number.isFinite(expiresAt) ||
      expiresAt <= nowMs ||
      typeof expiresAtValue !== 'string' ||
      typeof targetTimestamp !== 'string' ||
      !RESTORE_TIMESTAMP_RE.test(targetTimestamp)
    ) {
      return null;
    }
    return {
      version: 1,
      restore_id: restoreId,
      source,
      status_token: statusToken,
      expires_at: expiresAtValue,
      target_timestamp: targetTimestamp,
    };
  } catch {
    return null;
  }
}

/** Load the tab-scoped recovery record, removing malformed or expired state. */
export function loadRestoreRecovery(): RestoreRecoveryRecord | null {
  try {
    const raw = sessionStorage.getItem(RESTORE_RECOVERY_STORAGE_KEY);
    const record = parseRestoreRecoveryRecord(raw);
    if (raw !== null && record === null) {
      sessionStorage.removeItem(RESTORE_RECOVERY_STORAGE_KEY);
    }
    return record;
  } catch {
    return null;
  }
}

/** Atomically replace the tab-scoped recovery record when its shape is valid. */
export function saveRestoreRecovery(record: RestoreRecoveryRecord): boolean {
  const serialized = JSON.stringify(record);
  if (parseRestoreRecoveryRecord(serialized) === null) return false;
  try {
    sessionStorage.setItem(RESTORE_RECOVERY_STORAGE_KEY, serialized);
    return true;
  } catch {
    return false;
  }
}

/** Remove only the tab-scoped recovery record. */
export function clearRestoreRecovery(): void {
  try {
    sessionStorage.removeItem(RESTORE_RECOVERY_STORAGE_KEY);
  } catch {
    // Storage can be unavailable in hardened browser contexts.
  }
}

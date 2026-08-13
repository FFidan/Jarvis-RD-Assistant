import { z } from 'zod';

export const backupStoreSchema = z.enum([
  'jarvis',
  'litellm',
  'pdfs',
  'secrets',
  'qdrant',
]);

export const backupEntrySchema = z.looseObject({
  filename: z.string(),
  store: backupStoreSchema,
  size_bytes: z.number(),
  modified_at: z.string(),
  encrypted: z.boolean(),
});

export const backupStatusSchema = z.looseObject({
  backup_dir_available: z.boolean(),
  archive_count: z.number(),
  last_run_at: z.string().nullable(),
  trigger_pending: z.boolean(),
  last_attempt_at: z.string().nullable(),
  last_run_succeeded: z.boolean().nullable(),
  last_run_vectors_captured: z.boolean().nullable(),
  last_run_s3_complete: z.boolean().nullable(),
});

export const restorePointFileSchema = z.looseObject({
  filename: z.string(),
  store: backupStoreSchema,
  size_bytes: z.number(),
  encrypted: z.boolean(),
});

export const restorePointSchema = z.looseObject({
  timestamp: z.string(),
  created_at: z.string(),
  stores: z.array(backupStoreSchema),
  qdrant_collections: z.array(z.string()),
  complete: z.boolean(),
  has_pdfs: z.boolean(),
  legacy_missing_pdfs: z.boolean(),
  encrypted: z.boolean(),
  total_size_bytes: z.number(),
  files: z.array(restorePointFileSchema),
  app_version: z.string().nullable(),
  schema_version: z.number().nullable(),
  compat: z.enum(['same', 'older', 'newer', 'unknown']),
});

export const restoreSourceSchema = z.enum(['local', 'inbox']);

export const inboxRestorePointSchema = z.looseObject({
  timestamp: z.string(),
  complete: z.boolean(),
  has_secrets: z.boolean(),
  has_key: z.boolean(),
  has_pdfs: z.boolean(),
  legacy_missing_pdfs: z.boolean(),
});

export const restoreStatusSchema = z.looseObject({
  state: z.enum(['idle', 'pending', 'running', 'done', 'failed']),
  current_step: z.string().nullable(),
  steps: z.array(z.looseObject({
    name: z.string(),
    status: z.enum(['pending', 'running', 'done', 'failed', 'skipped', 'degraded']),
  })),
  safety_backup_ts: z.string().nullable(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  error: z.string().nullable(),
  manual_steps_required: z.boolean(),
  phase: z.string().nullable(),
  restore_id: z.string().nullable(),
  source: restoreSourceSchema.nullable(),
  quarantine: z.enum(['none', 'awaiting_review', 'unreadable']),
});

export const restoreRequestResponseSchema = z.looseObject({
  status: z.literal('scheduled'),
  status_token: z.string(),
  restore_id: z.string(),
  source: restoreSourceSchema,
  expires_at: z.string(),
});

export const restoreLastRunSchema = z.looseObject({
  attempted_at: z.string().nullable(),
  succeeded: z.boolean().nullable(),
  stores: z.record(z.string(), z.string()),
  vectors_captured: z.boolean().nullable(),
  s3_complete: z.boolean().nullable(),
});

export const restorePointsResponseSchema = z.looseObject({
  restore_points: z.array(restorePointSchema),
  retention_days: z.number().nullable(),
  last_run: restoreLastRunSchema.nullable(),
});

export const retentionConfigSchema = z.looseObject({
  keep_last_n: z.number().nullable(),
  max_age_days: z.number().nullable(),
});

export const scheduledResponseSchema = z.looseObject({ status: z.literal('scheduled') });
export const acknowledgedRestoreSchema = z.looseObject({
  status: z.literal('acknowledged'),
  restore_id: z.string(),
});
export const uploadGrantSchema = z.looseObject({
  grant_token: z.string(),
  expires_in_seconds: z.number(),
});

export type BackupEntry = z.infer<typeof backupEntrySchema>;
export type BackupStatus = z.infer<typeof backupStatusSchema>;
export type RestorePointFile = z.infer<typeof restorePointFileSchema>;
export type RestorePoint = z.infer<typeof restorePointSchema>;
export type RestoreSource = z.infer<typeof restoreSourceSchema>;
export type InboxRestorePoint = z.infer<typeof inboxRestorePointSchema>;
export type RestoreStatus = z.infer<typeof restoreStatusSchema>;
export type RestoreRequestResponse = z.infer<typeof restoreRequestResponseSchema>;
export type RestoreLastRun = z.infer<typeof restoreLastRunSchema>;
export type RestorePointsResponse = z.infer<typeof restorePointsResponseSchema>;
export type RetentionConfig = z.infer<typeof retentionConfigSchema>;
export type UploadGrant = z.infer<typeof uploadGrantSchema>;

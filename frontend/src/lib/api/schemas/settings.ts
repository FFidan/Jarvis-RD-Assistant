import { z } from 'zod';
import { jsonValueSchema } from './common';

export const topicSchema = z.looseObject({
  id: z.number(),
  name: z.string(),
  query_terms: z.array(z.string()),
  category: z.string().nullable(),
  description: z.string().nullable(),
  enabled: z.boolean(),
  created_at: z.string(),
});
export const topicListSchema = z.array(topicSchema);
export const topicSubscriptionListSchema = z.array(z.number());

export const sourceConfigSchema = z.looseObject({
  id: z.number(),
  source_type: z.string(),
  enabled: z.boolean(),
  config: z.record(z.string(), jsonValueSchema),
  priority: z.number(),
  display_order: z.number(),
  created_at: z.string(),
});
export const sourceConfigListSchema = z.array(sourceConfigSchema);

export const trackedAuthorSchema = z.looseObject({
  id: z.number(),
  author_name: z.string(),
  s2_author_id: z.string().nullable(),
  source: z.string(),
  enabled: z.boolean(),
  last_checked_at: z.string().nullable(),
  created_at: z.string(),
});
export const trackedAuthorListSchema = z.array(trackedAuthorSchema);
export const autoDetectAuthorsResponseSchema = z.looseObject({
  added: z.number(),
  already_tracked: z.number(),
});
export const checkTrackedAuthorsResponseSchema = z.looseObject({
  new_papers: z.number(),
  authors_checked: z.number(),
});

export const configEntrySchema = z.looseObject({
  key: z.string(),
  value: jsonValueSchema,
});
export const configEntryListSchema = z.array(configEntrySchema);

export const setupStatusSchema = z.looseObject({
  setup_completed: z.boolean(),
  models_ready: z.boolean(),
  models_downloading: z.array(z.string()),
  topics_count: z.number(),
  telegram_configured: z.boolean(),
  telegram_paired: z.boolean(),
  model_warnings: z.array(z.string()).optional(),
});

export const firstRunStatusSchema = z.looseObject({
  configured: z.boolean(),
  setup_completed: z.boolean().optional(),
  setup_mode: z.enum(['single', 'multi']).optional(),
  hw_tier_changed: z.boolean().optional(),
  hw_tier_baseline: z.string().nullable().optional(),
  hw_tier_current: z.string().nullable().optional(),
  gpu_vendor: z.string().optional(),
  access_mode: z.string().optional(),
  recommended_backend: z.string().nullable().optional(),
  current_backend: z.string().nullable().optional(),
  observed_backend: z.string().nullable().optional(),
  observed_recent_share: z.number().optional(),
  smtp_configured: z.boolean().optional(),
  smtp_reachable: z.boolean().optional(),
});

const firstRunServiceStatusSchema = z.looseObject({
  name: z.string(),
  ok: z.boolean(),
  detail: z.string().nullable(),
});

export const firstRunSystemCheckSchema = z.looseObject({
  services: z.array(firstRunServiceStatusSchema),
  all_ok: z.boolean(),
});

export const smtpSaveResponseSchema = z.looseObject({
  saved: z.boolean(),
  test_sent: z.boolean().nullable(),
  test_error: z.string().nullable(),
});

export const firstRunAdminResponseSchema = z.looseObject({
  id: z.number(),
  email: z.string(),
  role: z.string(),
});

export const firstRunCloudKeysResponseSchema = z.looseObject({
  saved_providers: z.array(z.string()),
  applied_now: z.array(z.string()),
  restart_required: z.boolean(),
});

export const telegramPairTokenResponseSchema = z.looseObject({
  token: z.string(),
  expires_at: z.string(),
});

export const userTelegramPairingStatusSchema = z.looseObject({
  paired: z.boolean(),
  chat_id: z.number().nullable(),
  telegram_username: z.string().nullable(),
  paired_at: z.string().nullable(),
});

export const nudgeSchema = z.looseObject({
  id: z.number(),
  nudge_type: z.string(),
  cron_expression: z.string(),
  enabled: z.boolean(),
  config: z.record(z.string(), jsonValueSchema),
  last_fired_at: z.string().nullable(),
  created_at: z.string(),
});
export const nudgeListSchema = z.array(nudgeSchema);

const extractionFieldSchema = z.looseObject({
  name: z.string(),
  label: z.string(),
  description: z.string(),
  type: z.string(),
});

export const extractionTemplateSchema = z.looseObject({
  id: z.number(),
  name: z.string(),
  description: z.string().nullable(),
  fields: z.array(extractionFieldSchema),
  is_default: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});
export const extractionTemplateListSchema = z.array(extractionTemplateSchema);

export const smtpConfigSchema = z.looseObject({
  host: z.string().nullable(),
  port: z.number().nullable(),
  user: z.string().nullable(),
  from_email: z.string().nullable(),
  reply_to: z.string().nullable(),
  from_name: z.string().nullable(),
  has_password: z.boolean(),
  restart_required: z.boolean().optional(),
  deliverable: z.boolean().optional(),
  issues: z.array(z.string()).optional(),
});

export const providerMetadataSchema = z.looseObject({
  id: z.string(),
  display_name: z.string(),
  kind: z.string(),
  api_key_config_key: z.string(),
  base_url_config_key: z.string().nullable(),
  assignment_prefix: z.string(),
  litellm_prefix: z.string(),
  privacy_boundary: z.string(),
  best_for: z.string(),
  data_note: z.string(),
  configured: z.boolean(),
  base_url_configured: z.boolean(),
  supports_assignment: z.boolean(),
  dashboard_url: z.string().url().nullable(),
  account_capability: z.enum(['current_key', 'unavailable']),
});
export const providerMetadataListSchema = z.array(providerMetadataSchema);

export const providerAccountResponseSchema = z.looseObject({
  provider: z.string(),
  capability: z.enum(['current_key', 'unavailable']),
  data: z.record(z.string(), z.union([z.boolean(), z.number(), z.string(), z.null()])),
  error_code: z.string().nullable(),
});

export const providerTestResponseSchema = z.looseObject({
  ok: z.boolean(),
  error: z.string().nullable(),
});

export const telegramBotTokenStatusSchema = z.looseObject({ has_token: z.boolean() });

export const telegramBotTokenSaveResponseSchema = z.looseObject({
  saved: z.boolean(),
  restart_required: z.boolean(),
});

export const setupModeResponseSchema = z.looseObject({
  mode: z.enum(['single', 'multi']),
  restart_required: z.boolean().optional(),
});

export type FirstRunStatus = z.infer<typeof firstRunStatusSchema>;
export type FirstRunServiceStatus = z.infer<typeof firstRunServiceStatusSchema>;
export type FirstRunSystemCheck = z.infer<typeof firstRunSystemCheckSchema>;
export type FirstRunSmtpResponse = z.infer<typeof smtpSaveResponseSchema>;
export type FirstRunAdminResponse = z.infer<typeof firstRunAdminResponseSchema>;
export type FirstRunCloudKeysResponse = z.infer<typeof firstRunCloudKeysResponseSchema>;
export type TelegramPairTokenResponse = z.infer<typeof telegramPairTokenResponseSchema>;
export type UserTelegramPairingStatus = z.infer<typeof userTelegramPairingStatusSchema>;
export type ProviderMetadata = z.infer<typeof providerMetadataSchema>;
export type ProviderAccountResponse = z.infer<typeof providerAccountResponseSchema>;

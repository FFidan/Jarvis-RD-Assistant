// Settings & configuration: topics, sources, tracked authors, config,
// setup/pairing, first-run wizard, Telegram, nudges, extraction templates,
// SMTP relay, per-source credentials, cloud LLM providers, access mode.
import { apiFetchJson, apiFetchRaw, apiFetchVoid, triggerBlobDownload } from './core';
import { okResponseSchema } from './schemas/common';
import {
  autoDetectAuthorsResponseSchema,
  checkTrackedAuthorsResponseSchema,
  configEntryListSchema,
  configEntrySchema,
  extractionTemplateListSchema,
  extractionTemplateSchema,
  firstRunAdminResponseSchema,
  firstRunCloudKeysResponseSchema,
  firstRunStatusSchema,
  firstRunSystemCheckSchema,
  nudgeListSchema,
  nudgeSchema,
  providerMetadataListSchema,
  providerTestResponseSchema,
  setupModeResponseSchema,
  setupStatusSchema,
  smtpConfigSchema,
  smtpSaveResponseSchema,
  sourceConfigListSchema,
  sourceConfigSchema,
  telegramBotTokenSaveResponseSchema,
  telegramBotTokenStatusSchema,
  telegramPairTokenResponseSchema,
  topicListSchema,
  topicSchema,
  topicSubscriptionListSchema,
  trackedAuthorListSchema,
  trackedAuthorSchema,
  userTelegramPairingStatusSchema,
} from './schemas/settings';
export type {
  FirstRunAdminResponse,
  FirstRunCloudKeysResponse,
  FirstRunServiceStatus,
  FirstRunSmtpResponse,
  FirstRunStatus,
  FirstRunSystemCheck,
  ProviderMetadata,
  TelegramPairTokenResponse,
  UserTelegramPairingStatus,
} from './schemas/settings';
import type {
  Topic,
  SourceConfig,
  TrackedAuthor,
  Nudge,
  ExtractionTemplate,
  SmtpConfigInput,
  SourceConfigPatch,
} from '@/types';

// --- Topics ---
export const fetchTopics = () => apiFetchJson('/api/topics', topicListSchema);
export const createTopic = (data: Partial<Topic>) =>
  apiFetchJson('/api/topics', topicSchema, { method: 'POST', body: JSON.stringify(data) });
export const updateTopic = (id: number, data: Partial<Topic>) =>
  apiFetchJson(`/api/topics/${id}`, topicSchema, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTopic = (id: number) =>
  apiFetchVoid(`/api/topics/${id}`, { method: 'DELETE' });
export const fetchMySubscriptions = () =>
  apiFetchJson('/api/topics/subscriptions', topicSubscriptionListSchema);
export const subscribeToTopic = (topicId: number) =>
  apiFetchVoid(`/api/topics/${topicId}/subscribe`, { method: 'PUT' });
export const unsubscribeFromTopic = (topicId: number) =>
  apiFetchVoid(`/api/topics/${topicId}/subscribe`, { method: 'DELETE' });

// --- Sources ---
export const fetchSources = () => apiFetchJson('/api/sources', sourceConfigListSchema);
export const updateSource = (id: number, data: Partial<SourceConfig>) =>
  apiFetchJson(`/api/sources/${id}`, sourceConfigSchema, { method: 'PUT', body: JSON.stringify(data) });
export const reorderSources = (source_types: string[]) =>
  apiFetchJson('/api/sources/reorder', sourceConfigListSchema, {
    method: 'PATCH',
    body: JSON.stringify({ source_types }),
    headers: { 'Content-Type': 'application/json' },
  });

// --- Tracked Authors ---
export const fetchTrackedAuthors = () => apiFetchJson('/api/authors', trackedAuthorListSchema);
export const createTrackedAuthor = (data: Partial<TrackedAuthor>) =>
  apiFetchJson('/api/authors', trackedAuthorSchema, { method: 'POST', body: JSON.stringify(data) });
export const updateTrackedAuthor = (id: number, data: Partial<TrackedAuthor>) =>
  apiFetchJson(`/api/authors/${id}`, trackedAuthorSchema, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTrackedAuthor = (id: number) =>
  apiFetchVoid(`/api/authors/${id}`, { method: 'DELETE' });
export const autoDetectAuthors = () =>
  apiFetchJson('/api/authors/auto-detect', autoDetectAuthorsResponseSchema, { method: 'POST' });
export const checkTrackedAuthors = () =>
  apiFetchJson('/api/authors/check', checkTrackedAuthorsResponseSchema, { method: 'POST' });

// --- Account data export ---
const ACCOUNT_EXPORT_FILENAME = 'jarvis-data-export.zip';

export async function downloadMyData(): Promise<void> {
  const res = await apiFetchRaw('/api/me/export');
  const blob = await res.blob();
  triggerBlobDownload(blob, ACCOUNT_EXPORT_FILENAME);
}

// --- Settings / Config ---
export const fetchConfig = () => apiFetchJson('/api/config', configEntryListSchema);
export const setConfig = (key: string, value: unknown) =>
  apiFetchJson(`/api/config/${key}`, configEntrySchema, { method: 'PUT', body: JSON.stringify({ key, value }) });

// --- Setup / Pairing ---
export const getSetupStatus = () =>
  apiFetchJson('/api/system/setup-status', setupStatusSchema);

// --- First-run wizard (pre-auth bootstrap) ---
// These call /api/setup/* which is unauthenticated until the first admin exists.
// Distinct surface from /api/system/setup-status above (post-login bootstrap).
export interface FirstRunSmtpBody {
  host: string;
  port: number;
  user?: string | null;
  pass?: string | null;
  from_email: string;
  /** Optional sender identity. Omit to keep; '' to clear. snake_case (no alias). */
  reply_to?: string | null;
  from_name?: string | null;
  test_send?: boolean;
  test_recipient?: string | null;
}
export interface FirstRunCloudKeysBody {
  openai?: string | null;
  anthropic?: string | null;
  gemini?: string | null;
}
export const getFirstRunStatus = () =>
  apiFetchJson('/api/setup/status', firstRunStatusSchema);

export const dismissBanner = (banner_kind: string) =>
  apiFetchVoid('/api/settings/ai/dismiss-banner', {
    method: 'POST',
    body: JSON.stringify({ banner_kind }),
  });

// The first-run wizard authorizes these unauthenticated POSTs with the
// bootstrap setup token (printed by setup.sh / scripts/jarvis-setup.sh,
// captured from the URL or pasted into the wizard). The header is sent only
// when a token is present; an unconfigured install without a token is open in
// development but fails closed (403) in production.
const setupTokenHeader = (token?: string | null): Record<string, string> =>
  token ? { 'X-Setup-Token': token } : {};

export const runFirstRunSystemCheck = (setupToken?: string | null) =>
  apiFetchJson('/api/setup/system-check', firstRunSystemCheckSchema, {
    method: 'POST',
    headers: setupTokenHeader(setupToken),
  });

export const saveFirstRunSmtp = (body: FirstRunSmtpBody, setupToken?: string | null) =>
  apiFetchJson('/api/setup/smtp', smtpSaveResponseSchema, {
    method: 'POST',
    body: JSON.stringify(body),
    headers: setupTokenHeader(setupToken),
  });

export const createFirstRunAdmin = (email: string, setupToken?: string | null) =>
  apiFetchJson('/api/setup/admin', firstRunAdminResponseSchema, {
    method: 'POST',
    body: JSON.stringify({ email }),
    headers: setupTokenHeader(setupToken),
  });

export const saveFirstRunCloudKeys = (body: FirstRunCloudKeysBody, setupToken?: string | null) =>
  apiFetchJson('/api/setup/cloud-llm-keys', firstRunCloudKeysResponseSchema, {
    method: 'POST',
    body: JSON.stringify(body),
    headers: setupTokenHeader(setupToken),
  });

// --- Per-user multi-tenant Telegram pairing ---

/** Issue a 15-minute per-user pairing token. Requires an authenticated session. */
export const requestTelegramPairToken = () =>
  apiFetchJson('/api/telegram/pair-token', telegramPairTokenResponseSchema, { method: 'POST' });

/** Return the current user's Telegram pairing status from telegram_user_pairings. */
export const getTelegramPairing = () =>
  apiFetchJson('/api/telegram/pairing', userTelegramPairingStatusSchema);

/** Remove the current user's Telegram pairing. */
export const removeTelegramPairing = () =>
  apiFetchVoid('/api/telegram/pairing', { method: 'DELETE' });

export const markSetupCompleted = () =>
  apiFetchVoid('/api/config/setup.completed', {
    method: 'PUT',
    body: JSON.stringify({ key: 'setup.completed', value: true }),
  });

// --- Nudges ---
export const fetchNudges = () => apiFetchJson('/api/nudges', nudgeListSchema);
export const updateNudge = (id: number, data: Partial<Nudge>) =>
  apiFetchJson(`/api/nudges/${id}`, nudgeSchema, { method: 'PUT', body: JSON.stringify(data) });

// --- Extraction Templates ---
export const fetchExtractionTemplates = () =>
  apiFetchJson('/api/extraction-templates', extractionTemplateListSchema);
export const createExtractionTemplate = (data: Partial<ExtractionTemplate>) =>
  apiFetchJson('/api/extraction-templates', extractionTemplateSchema, { method: 'POST', body: JSON.stringify(data) });
export const updateExtractionTemplate = (id: number, data: Partial<ExtractionTemplate>) =>
  apiFetchJson(`/api/extraction-templates/${id}`, extractionTemplateSchema, { method: 'PUT', body: JSON.stringify(data) });
export const deleteExtractionTemplate = (id: number) =>
  apiFetchVoid(`/api/extraction-templates/${id}`, { method: 'DELETE' });

// --- Settings: SMTP relay ---

export const getSmtpConfig = () => apiFetchJson('/api/setup/smtp', smtpConfigSchema);

/** Mirrors setup.py SmtpBody (populate_by_name → `password` is accepted). */
export const saveSmtpConfig = (body: SmtpConfigInput) =>
  apiFetchJson(
    '/api/setup/smtp',
    smtpSaveResponseSchema,
    { method: 'POST', body: JSON.stringify(body) },
  );

// --- Settings: per-source credential / cooldown ---

export const patchSourceConfig = (sourceType: string, patch: SourceConfigPatch) =>
  apiFetchJson(`/api/settings/sources/${sourceType}`, okResponseSchema, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });

export const clearSourceCooldown = (sourceType: string) =>
  apiFetchJson(`/api/settings/sources/${sourceType}/clear-cooldown`, okResponseSchema, {
    method: 'POST',
  });

// --- Cloud LLM Providers ---

export type CloudProvider = string;

export const CLOUD_PROVIDER_DISPLAY_ORDER = [
  'anthropic',
  'openai',
  'google',
  'openrouter',
  'deepseek',
  'mistral',
  'moonshot',
  'zai',
  'custom_openai_compatible',
] as const;

const CLOUD_PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  google: 'Google Gemini',
  openrouter: 'OpenRouter',
  deepseek: 'DeepSeek',
  mistral: 'Mistral AI',
  moonshot: 'Kimi / Moonshot',
  zai: 'Z.ai / GLM',
  custom_openai_compatible: 'Custom OpenAI-compatible endpoint',
};

export function cloudProviderLabel(provider: string): string {
  return CLOUD_PROVIDER_LABELS[provider] ?? provider;
}

export function compareCloudProviders(a: string, b: string): number {
  const ia = CLOUD_PROVIDER_DISPLAY_ORDER.indexOf(a as (typeof CLOUD_PROVIDER_DISPLAY_ORDER)[number]);
  const ib = CLOUD_PROVIDER_DISPLAY_ORDER.indexOf(b as (typeof CLOUD_PROVIDER_DISPLAY_ORDER)[number]);
  const oa = ia === -1 ? CLOUD_PROVIDER_DISPLAY_ORDER.length : ia;
  const ob = ib === -1 ? CLOUD_PROVIDER_DISPLAY_ORDER.length : ib;
  return oa - ob || a.localeCompare(b);
}

/** Return non-secret provider metadata and configured statuses. */
export async function listProviders() {
  return apiFetchJson('/api/providers', providerMetadataListSchema);
}

/**
 * Returns the masked key value for each cloud provider (e.g. "sk-a****").
 * Null if no key is stored.
 */
export async function getProviderStatuses(): Promise<Record<CloudProvider, string | null>> {
  const [configs, providers] = await Promise.all([
    fetchConfig(),
    listProviders(),
  ]);
  const result: Record<CloudProvider, string | null> = {};
  for (const provider of providers) {
    const entry = configs.find((c) => c.key === provider.api_key_config_key);
    if (entry != null && entry.value != null) {
      const v = entry.value;
      result[provider.id] = typeof v === 'string' ? v.replace(/^"|"$/g, '') : String(v);
    } else {
      result[provider.id] = null;
    }
  }
  return result;
}

/** Save a cloud provider API key via the unified config endpoint. */
export async function setProviderKey(
  provider: CloudProvider,
  apiKey: string,
  configKey?: string,
): Promise<void> {
  await setConfig(configKey ?? `llm.${provider}.api_key`, apiKey);
}

/** Test connectivity for a cloud provider. */
export async function testProvider(
  provider: CloudProvider,
) {
  return apiFetchJson(`/api/providers/${provider}/test`, providerTestResponseSchema, { method: 'POST' });
}

// --- Settings: Telegram bot token (UI-4) ---

/** Check whether a Telegram bot token is stored. Token value is never returned. */
export const getTelegramBotToken = () =>
  apiFetchJson('/api/setup/telegram-bot-token', telegramBotTokenStatusSchema);

/** Save a new Telegram bot token. A restart is required for it to take effect. */
export const saveTelegramBotToken = (token: string) =>
  apiFetchJson('/api/setup/telegram-bot-token', telegramBotTokenSaveResponseSchema, {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

// --- Settings: access mode (UI-5) ---

/** Switch the sign-in method offered (single-user API-key vs multi-user magic-link). Applied on the next status poll. */
export const saveSetupMode = (mode: 'single' | 'multi') =>
  apiFetchJson('/api/setup/mode', setupModeResponseSchema, {
    method: 'POST',
    body: JSON.stringify({ mode }),
  });

// Settings & configuration: topics, sources, tracked authors, config,
// setup/pairing, first-run wizard, Telegram, nudges, extraction templates,
// SMTP relay, per-source credentials, cloud LLM providers, access mode.
import { apiFetch, apiFetchRaw, triggerBlobDownload } from './core';
import type {
  Topic,
  SourceConfig,
  TrackedAuthor,
  ConfigEntry,
  Nudge,
  ExtractionTemplate,
  SetupStatus,
  SmtpConfig,
  SmtpConfigInput,
  SourceConfigPatch,
  TelegramBotTokenStatus,
  TelegramBotTokenSaveResponse,
  SetupModeResponse,
} from '@/types';

// --- Topics ---
export const fetchTopics = () => apiFetch<Topic[]>('/api/topics');
export const createTopic = (data: Partial<Topic>) =>
  apiFetch<Topic>('/api/topics', { method: 'POST', body: JSON.stringify(data) });
export const updateTopic = (id: number, data: Partial<Topic>) =>
  apiFetch<Topic>(`/api/topics/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTopic = (id: number) =>
  apiFetch<void>(`/api/topics/${id}`, { method: 'DELETE' });
export const fetchMySubscriptions = () =>
  apiFetch<number[]>('/api/topics/subscriptions');
export const subscribeToTopic = (topicId: number) =>
  apiFetch<void>(`/api/topics/${topicId}/subscribe`, { method: 'PUT' });
export const unsubscribeFromTopic = (topicId: number) =>
  apiFetch<void>(`/api/topics/${topicId}/subscribe`, { method: 'DELETE' });

// --- Sources ---
export const fetchSources = () => apiFetch<SourceConfig[]>('/api/sources');
export const updateSource = (id: number, data: Partial<SourceConfig>) =>
  apiFetch<SourceConfig>(`/api/sources/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const reorderSources = (source_types: string[]) =>
  apiFetch<SourceConfig[]>('/api/sources/reorder', {
    method: 'PATCH',
    body: JSON.stringify({ source_types }),
    headers: { 'Content-Type': 'application/json' },
  });

// --- Tracked Authors ---
export const fetchTrackedAuthors = () => apiFetch<TrackedAuthor[]>('/api/authors');
export const createTrackedAuthor = (data: Partial<TrackedAuthor>) =>
  apiFetch<TrackedAuthor>('/api/authors', { method: 'POST', body: JSON.stringify(data) });
export const updateTrackedAuthor = (id: number, data: Partial<TrackedAuthor>) =>
  apiFetch<TrackedAuthor>(`/api/authors/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTrackedAuthor = (id: number) =>
  apiFetch<void>(`/api/authors/${id}`, { method: 'DELETE' });
export const autoDetectAuthors = () =>
  apiFetch<{ added: number; already_tracked: number }>('/api/authors/auto-detect', { method: 'POST' });
export const checkTrackedAuthors = () =>
  apiFetch<{ new_papers: number; authors_checked: number }>('/api/authors/check', { method: 'POST' });

// --- Account data export ---
const ACCOUNT_EXPORT_FILENAME = 'jarvis-data-export.zip';

export async function downloadMyData(): Promise<void> {
  const res = await apiFetchRaw('/api/me/export');
  const blob = await res.blob();
  triggerBlobDownload(blob, ACCOUNT_EXPORT_FILENAME);
}

// --- Settings / Config ---
export const fetchConfig = () => apiFetch<ConfigEntry[]>('/api/config');
export const setConfig = (key: string, value: unknown) =>
  apiFetch<ConfigEntry>(`/api/config/${key}`, { method: 'PUT', body: JSON.stringify({ key, value }) });

// --- Setup / Pairing ---
export const getSetupStatus = () =>
  apiFetch<SetupStatus>('/api/system/setup-status');

// --- First-run wizard (pre-auth bootstrap) ---
// These call /api/setup/* which is unauthenticated until the first admin exists.
// Distinct surface from /api/system/setup-status above (post-login bootstrap).
export interface FirstRunStatus {
  configured: boolean;
  /**
   * True once the onboarding wizard has been completed end-to-end (the
   * `setup.completed` config flag). Distinct from `configured` (an admin user
   * exists): a CLI-bootstrapped install can be `configured` yet not yet
   * `setup_completed`. The unified onboarding gate keys on this field.
   * Added to the pre-auth /api/setup/status payload (Task A1).
   */
  setup_completed?: boolean;
  setup_mode?: 'single' | 'multi';
  /** True when JARVIS_HW_TIER in .env differs from the baseline recorded at last boot. */
  hw_tier_changed?: boolean;
  hw_tier_baseline?: string | null;
  hw_tier_current?: string | null;
  recommended_backend?: string | null;
  current_backend?: string | null;
  observed_backend?: string | null;
  observed_recent_share?: number;
  /**
   * True iff SMTP is configured (DB or env). When false, magic-links cannot be
   * delivered and the login page defaults to the API-key tab. Optional so older
   * backends (before Task T0.4) degrade gracefully — absence is treated as
   * unknown (no default-override applied).
   */
  smtp_configured?: boolean;
  /**
   * True iff the configured relay currently accepts a connection (cached
   * liveness probe). `smtp_configured` is presence-only, so a relay can be
   * configured yet unreachable; LoginPage surfaces that "configured but
   * failing" state from this field. Optional — older backends omit it.
   */
  smtp_reachable?: boolean;
}
export interface FirstRunServiceStatus { name: string; ok: boolean; detail: string | null }
export interface FirstRunSystemCheck { services: FirstRunServiceStatus[]; all_ok: boolean }
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
export interface FirstRunSmtpResponse {
  saved: boolean;
  test_sent: boolean | null;
  test_error: string | null;
}
export interface FirstRunAdminResponse { id: number; email: string; role: string }
export interface FirstRunCloudKeysBody {
  openai?: string | null;
  anthropic?: string | null;
  gemini?: string | null;
}
export interface FirstRunCloudKeysResponse {
  saved_providers: string[];
  /** Providers whose keys were applied to the running process immediately. */
  applied_now: string[];
  /** True when a service restart is needed for changes to take effect. */
  restart_required: boolean;
}

export const getFirstRunStatus = () =>
  apiFetch<FirstRunStatus>('/api/setup/status');

export const dismissBanner = (banner_kind: string) =>
  apiFetch<void>('/api/settings/ai/dismiss-banner', {
    method: 'POST',
    body: JSON.stringify({ banner_kind }),
  });

// The first-run wizard authorizes these unauthenticated POSTs with the
// bootstrap setup token (printed by setup.sh, captured from the URL). The
// header is sent only when a token is present; an unconfigured/legacy install
// without a token is treated as open by the backend.
const setupTokenHeader = (token?: string | null): Record<string, string> =>
  token ? { 'X-Setup-Token': token } : {};

export const runFirstRunSystemCheck = (setupToken?: string | null) =>
  apiFetch<FirstRunSystemCheck>('/api/setup/system-check', {
    method: 'POST',
    headers: setupTokenHeader(setupToken),
  });

export const saveFirstRunSmtp = (body: FirstRunSmtpBody, setupToken?: string | null) =>
  apiFetch<FirstRunSmtpResponse>('/api/setup/smtp', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: setupTokenHeader(setupToken),
  });

export const createFirstRunAdmin = (email: string, setupToken?: string | null) =>
  apiFetch<FirstRunAdminResponse>('/api/setup/admin', {
    method: 'POST',
    body: JSON.stringify({ email }),
    headers: setupTokenHeader(setupToken),
  });

export const saveFirstRunCloudKeys = (body: FirstRunCloudKeysBody, setupToken?: string | null) =>
  apiFetch<FirstRunCloudKeysResponse>('/api/setup/cloud-llm-keys', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: setupTokenHeader(setupToken),
  });

// --- Per-user multi-tenant Telegram pairing ---

export interface TelegramPairTokenResponse {
  token: string;
  expires_at: string;
}

export interface UserTelegramPairingStatus {
  paired: boolean;
  chat_id: number | null;
  telegram_username: string | null;
  paired_at: string | null;
}

/** Issue a 15-minute per-user pairing token. Requires an authenticated session. */
export const requestTelegramPairToken = () =>
  apiFetch<TelegramPairTokenResponse>('/api/telegram/pair-token', { method: 'POST' });

/** Return the current user's Telegram pairing status from telegram_user_pairings. */
export const getTelegramPairing = () =>
  apiFetch<UserTelegramPairingStatus>('/api/telegram/pairing');

/** Remove the current user's Telegram pairing. */
export const removeTelegramPairing = () =>
  apiFetch<void>('/api/telegram/pairing', { method: 'DELETE' });

export const markSetupCompleted = () =>
  apiFetch<void>('/api/config/setup.completed', {
    method: 'PUT',
    body: JSON.stringify({ key: 'setup.completed', value: true }),
  });

// --- Nudges ---
export const fetchNudges = () => apiFetch<Nudge[]>('/api/nudges');
export const updateNudge = (id: number, data: Partial<Nudge>) =>
  apiFetch<Nudge>(`/api/nudges/${id}`, { method: 'PUT', body: JSON.stringify(data) });

// --- Extraction Templates ---
export const fetchExtractionTemplates = () =>
  apiFetch<ExtractionTemplate[]>('/api/extraction-templates');
export const createExtractionTemplate = (data: Partial<ExtractionTemplate>) =>
  apiFetch<ExtractionTemplate>('/api/extraction-templates', { method: 'POST', body: JSON.stringify(data) });
export const updateExtractionTemplate = (id: number, data: Partial<ExtractionTemplate>) =>
  apiFetch<ExtractionTemplate>(`/api/extraction-templates/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteExtractionTemplate = (id: number) =>
  apiFetch<void>(`/api/extraction-templates/${id}`, { method: 'DELETE' });

// --- Settings: SMTP relay ---

export const getSmtpConfig = () => apiFetch<SmtpConfig>('/api/setup/smtp');

/** Mirrors setup.py SmtpBody (populate_by_name → `password` is accepted). */
export const saveSmtpConfig = (body: SmtpConfigInput) =>
  apiFetch<{ saved: boolean; test_sent: boolean | null; test_error: string | null }>(
    '/api/setup/smtp',
    { method: 'POST', body: JSON.stringify(body) },
  );

// --- Settings: per-source credential / cooldown ---

export const patchSourceConfig = (sourceType: string, patch: SourceConfigPatch) =>
  apiFetch<{ ok: boolean }>(`/api/settings/sources/${sourceType}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });

export const clearSourceCooldown = (sourceType: string) =>
  apiFetch<{ ok: boolean }>(`/api/settings/sources/${sourceType}/clear-cooldown`, {
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

export type ProviderMetadata = {
  id: CloudProvider;
  display_name: string;
  kind: 'direct' | 'router' | 'self_hosted' | string;
  api_key_config_key: string;
  base_url_config_key: string | null;
  assignment_prefix: string;
  litellm_prefix: string;
  privacy_boundary: string;
  best_for: string;
  data_note: string;
  configured: boolean;
  base_url_configured: boolean;
  supports_assignment: boolean;
};

/** Return non-secret provider metadata and configured statuses. */
export async function listProviders(): Promise<ProviderMetadata[]> {
  return apiFetch<ProviderMetadata[]>('/api/providers');
}

/**
 * Returns the masked key value for each cloud provider (e.g. "sk-a****").
 * Null if no key is stored.
 */
export async function getProviderStatuses(): Promise<Record<CloudProvider, string | null>> {
  const [configs, providers] = await Promise.all([
    apiFetch<Array<{ key: string; value: unknown }>>('/api/config'),
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
): Promise<{ ok: boolean; error: string | null }> {
  return apiFetch(`/api/providers/${provider}/test`, { method: 'POST' });
}

// --- Settings: Telegram bot token (UI-4) ---

/** Check whether a Telegram bot token is stored. Token value is never returned. */
export const getTelegramBotToken = () =>
  apiFetch<TelegramBotTokenStatus>('/api/setup/telegram-bot-token');

/** Save a new Telegram bot token. A restart is required for it to take effect. */
export const saveTelegramBotToken = (token: string) =>
  apiFetch<TelegramBotTokenSaveResponse>('/api/setup/telegram-bot-token', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

// --- Settings: access mode (UI-5) ---

/** Switch the sign-in method offered (single-user API-key vs multi-user magic-link). Applied on the next status poll. */
export const saveSetupMode = (mode: 'single' | 'multi') =>
  apiFetch<SetupModeResponse>('/api/setup/mode', {
    method: 'POST',
    body: JSON.stringify({ mode }),
  });

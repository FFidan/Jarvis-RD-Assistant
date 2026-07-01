export interface SourceConfig {
  id: number;
  source_type: string;
  enabled: boolean;
  config: Record<string, unknown>;
  priority: number;
  display_order: number;
  created_at: string;
}

export interface ConfigEntry {
  key: string;
  value: unknown;
}

export interface Nudge {
  id: number;
  nudge_type: string;
  cron_expression: string;
  enabled: boolean;
  config: Record<string, unknown>;
  last_fired_at: string | null;
  created_at: string;
}

export interface SetupStatus {
  setup_completed: boolean;
  models_ready: boolean;
  models_downloading: string[];
  topics_count: number;
  telegram_configured: boolean;
  telegram_paired: boolean;
  model_warnings?: string[];
}

export interface ModelFitDetail {
  default: 'fits' | 'partial' | 'unfit' | 'cloud' | 'unknown';
  at_num_ctx: number;
  required_vram_gb: number | null;
  default_num_ctx: number;
  max_num_ctx: number;
  kv_cache_bytes_per_token: number | null;
}

export interface CloudLlmKeysResponse {
  saved_providers: string[];
  applied_now: string[];
  restart_required: boolean;
}

export interface TelegramBotTokenStatus {
  has_token: boolean;
}

export interface TelegramBotTokenSaveResponse {
  saved: boolean;
  restart_required: boolean;
}

export interface SetupModeResponse {
  mode: 'single' | 'multi';
  restart_required?: boolean;
}

export interface SmtpConfig {
  host: string | null;
  port: number | null;
  user: string | null;
  from_email: string | null;
  reply_to: string | null;
  from_name: string | null;
  has_password: boolean;
  restart_required?: boolean;
  deliverable?: boolean;
  issues?: string[];
}

export interface SmtpConfigInput {
  host: string;
  port: number;
  user: string;
  password: string;
  from_email: string;
  reply_to?: string;
  from_name?: string;
  test_send?: boolean;
  test_recipient?: string;
}

export interface SourceConfigPatch {
  api_key?: string;
  email?: string;
}

export interface SystemCapabilities {
  networkx: boolean;
  scikit_learn: boolean;
  structured_output_enforced: boolean;
}

export interface AccountResponse {
  id: number;
  email: string;
  role: string;
  display_name: string | null;
  created_at: string;
  last_login_at: string | null;
}

export interface AccountUpdateResponse {
  account: AccountResponse;
  email_verification_sent: boolean;
}

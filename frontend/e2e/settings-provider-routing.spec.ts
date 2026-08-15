import { expect, test, type Page, type Route } from '@playwright/test';
import { installMockedApiDefaults, RETURNING_USER_PREFERENCES } from './helpers/setup';

const PROVIDERS = [
  {
    id: 'anthropic',
    display_name: 'Anthropic',
    kind: 'direct',
    api_key_config_key: 'llm.anthropic.api_key',
    base_url_config_key: null,
    assignment_prefix: 'anthropic/',
    litellm_prefix: 'anthropic/',
    privacy_boundary: 'direct_provider',
    best_for: 'Long-context reading and careful synthesis.',
    data_note: 'Relevant prompts and excerpts are sent to Anthropic when assigned.',
    configured: false,
    base_url_configured: false,
    supports_assignment: true,
    dashboard_url: 'https://console.anthropic.com/',
    account_capability: 'unavailable',
  },
  {
    id: 'openai',
    display_name: 'OpenAI',
    kind: 'direct',
    api_key_config_key: 'llm.openai.api_key',
    base_url_config_key: null,
    assignment_prefix: 'openai/',
    litellm_prefix: 'openai/',
    privacy_boundary: 'direct_provider',
    best_for: 'Structured extraction and synthesis.',
    data_note: 'Relevant prompts and excerpts are sent to OpenAI when assigned.',
    configured: true,
    base_url_configured: false,
    supports_assignment: true,
    dashboard_url: 'https://platform.openai.com/api-keys',
    account_capability: 'unavailable',
  },
  {
    id: 'openrouter',
    display_name: 'OpenRouter',
    kind: 'router',
    api_key_config_key: 'llm.providers.openrouter.api_key',
    base_url_config_key: null,
    assignment_prefix: 'openrouter/',
    litellm_prefix: 'openrouter/',
    privacy_boundary: 'router',
    best_for: 'One key for many hosted models.',
    data_note: 'Requests pass through OpenRouter when assigned.',
    configured: false,
    base_url_configured: false,
    supports_assignment: true,
    dashboard_url: 'https://openrouter.ai/settings/keys',
    account_capability: 'current_key',
  },
  {
    id: 'custom_openai_compatible',
    display_name: 'Custom OpenAI-compatible endpoint',
    kind: 'self_hosted',
    api_key_config_key: 'llm.providers.custom_openai_compatible.api_key',
    base_url_config_key: 'llm.providers.custom_openai_compatible.base_url',
    assignment_prefix: 'custom_openai/',
    litellm_prefix: 'openai/',
    privacy_boundary: 'self_hosted',
    best_for: 'Trusted self-hosted or institutional gateways.',
    data_note: 'Requests are sent to the configured endpoint when assigned.',
    configured: false,
    base_url_configured: false,
    supports_assignment: true,
    dashboard_url: null,
    account_capability: 'unavailable',
  },
];

const CONFIG = [
  ...RETURNING_USER_PREFERENCES,
  { key: 'llm.openai.api_key', value: 'masked-key' },
  { key: 'llm.smart_model', value: 'qwen3:14b' },
  { key: 'llm.fast_model', value: 'qwen3:4b' },
];

const LOCAL_FIT_DETAIL = {
  default: 'fits',
  at_num_ctx: 32768,
  required_vram_gb: 10,
  base_vram_gb: 10,
  base_num_ctx: 32768,
  default_num_ctx: 32768,
  max_num_ctx: 32768,
  kv_cache_bytes_per_token: null,
};

const CLOUD_FIT_DETAIL = {
  default: 'cloud',
  at_num_ctx: 128000,
  required_vram_gb: null,
  base_vram_gb: null,
  base_num_ctx: 128000,
  default_num_ctx: 128000,
  max_num_ctx: 128000,
  kv_cache_bytes_per_token: null,
};

const SYSTEM_MODELS = {
  status: 'ok',
  installed: [],
  hardware: { vram_gb: 24, vram_source: 'nvidia-smi', tier: 3 },
  current: { smart_model: 'qwen3:14b', fast_model: 'qwen3:4b' },
  issues: {},
  catalog: [
    {
      id: 'qwen3:14b', name: 'Qwen3 14B', provider: 'ollama', ollama_tag: 'qwen3:14b', roles: ['smart'],
      vram_gb: 10, disk_gb: 9, context_tokens: 32768, license: 'Apache 2.0', tier: 3,
      description: 'Strong local model.', notes: '', last_reviewed: '2026-07-06', status: 'active',
      embedding_dimension: null, phase: 'active', assignable: true,
      min_vram_gb_at_default_ctx: 10, kv_cache_bytes_per_token: null,
      default_num_ctx: 32768, max_num_ctx: 32768, supports_thinking: true,
      active: true, pulled: true, provider_key_present: null, fit: 'recommended', can_assign: true, assign_blocker: null,
      fit_detail: LOCAL_FIT_DETAIL,
    },
    {
      id: 'openai/gpt-4o', name: 'GPT-4o', provider: 'openai', ollama_tag: null, roles: ['smart'],
      vram_gb: 0, disk_gb: 0, context_tokens: 128000, license: 'Commercial', tier: 0,
      description: 'Cloud model.', notes: '', last_reviewed: '2026-07-06', status: 'cloud_required',
      embedding_dimension: null, phase: 'active', assignable: true,
      min_vram_gb_at_default_ctx: null, kv_cache_bytes_per_token: null,
      default_num_ctx: null, max_num_ctx: null, supports_thinking: false,
      active: false, pulled: false, provider_key_present: true, fit: 'available', can_assign: true, assign_blocker: null,
      fit_detail: CLOUD_FIT_DETAIL,
      input_price_per_million: '2.5', output_price_per_million: '10', price_source: 'openrouter',
    },
    {
      id: 'openrouter/meta-llama/llama-3.1-70b-instruct', name: 'OpenRouter Llama 70B', provider: 'openrouter', ollama_tag: null, roles: ['smart'],
      vram_gb: 0, disk_gb: 0, context_tokens: 128000, license: 'Commercial', tier: 0,
      description: 'Router model.', notes: '', last_reviewed: '2026-07-06', status: 'cloud_required',
      embedding_dimension: null, phase: 'active', assignable: true,
      min_vram_gb_at_default_ctx: null, kv_cache_bytes_per_token: null,
      default_num_ctx: null, max_num_ctx: null, supports_thinking: false,
      active: false, pulled: false, provider_key_present: false, fit: 'key_required', can_assign: false,
      assign_blocker: 'Add an OpenRouter API key before assigning this model.',
      fit_detail: CLOUD_FIT_DETAIL,
      input_price_per_million: null, output_price_per_million: null, price_source: null,
    },
  ],
  recommendations: {},
  reviewed_choices: {},
  hardware_recommendation: {
    vram_mb: 24576,
    bucket: 'MID_HIGH',
    summary: 'Test recommendation',
    aliases: [],
  },
  delivery: { smart: 'applied', fast: 'applied' },
  routing: { smart: 'qwen3:14b', fast: 'qwen3:4b' },
  consistent: true,
  provider_lists: {
    openai: { model_count: 1, fetched_at: '2026-08-11T08:00:00Z', error: null, truncated: false, excluded: {} },
  },
  embedding_contract: {
    model: 'qwen3-embedding:4b',
    dimension: 2560,
    change_requires_reindex: true,
  },
};

async function seedAdminSession(page: Page) {
  await page.addInitScript(() => {
    const state = {
      state: {
        isAuthenticated: true,
        authTime: Date.now(),
        user: { id: 1, email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
    window.localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });
}

async function setupProviderMocks(page: Page) {
  await seedAdminSession(page);
  await installMockedApiDefaults(page);

  await page.route('**/api/account', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 1, email: 'admin@example.com', role: 'admin', display_name: 'Admin' }) });
  });
  await page.route('**/api/sources', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/config', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CONFIG) });
  });
  await page.route('**/api/config/**', async (route: Route) => {
    if (new URL(route.request().url()).pathname.startsWith('/api/config/ui.')) {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });
  await page.route('**/api/providers', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PROVIDERS) });
  });
  await page.route('**/api/providers/**/test', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, error: null }) });
  });
  await page.route('**/api/system/models', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SYSTEM_MODELS) });
  });
}

test.describe('AI provider setup and model routing @settings-ia', () => {
  test('provider setup uses add/manage flow with custom endpoint fields', async ({ page }) => {
    await setupProviderMocks(page);
    await page.goto('/settings?section=models&item=providers');

    await expect(page.getByRole('heading', { name: 'Providers & Routing', level: 2 })).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('heading', { name: 'Providers & Routing', level: 3 })).toBeVisible();
    await expect(page.getByRole('button', { name: /OpenAI Connected .*1 model available/ })).toBeVisible();

    await page.getByRole('button', { name: 'Add cloud provider' }).click();
    await expect(page.getByText('Recommended routers')).toBeVisible();
    await expect(page.getByRole('button', { name: /OpenRouter/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Custom OpenAI-compatible endpoint/ })).toBeVisible();

    await page.getByRole('button', { name: /Custom OpenAI-compatible endpoint/ }).click();
    await expect(page.getByText('Trusted self-hosted or institutional gateways.')).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Custom OpenAI-compatible endpoint stored key status' })).toBeVisible();
    await expect(page.getByLabel('Base URL')).toBeVisible();
    await expect(page.getByText(/Provider keys are deployment-wide and encrypted at rest/)).toBeVisible();
  });

  test('model picker separates provider catalogs and keeps blocked routes explicit', async ({ page }) => {
    await setupProviderMocks(page);
    await page.goto('/settings?section=models&item=llm');

    await expect(page.getByRole('heading', { name: 'AI models', level: 2 })).toBeVisible({ timeout: 8000 });
    await page.getByTestId('change-model-smart').click();
    await expect(page.getByRole('heading', { name: 'Choose a Main model' })).toBeVisible();
    await page.getByRole('button', { name: 'OpenAI, 1 model' }).click();
    await expect(page.getByText('GPT-4o', { exact: true })).toBeVisible();
    await expect(page.getByText('$2.5 input / $10 output per 1M tokens')).toBeVisible();
    await page.getByRole('button', { name: 'OpenRouter, 1 model' }).click();
    await expect(page.getByText('OpenRouter Llama 70B')).toBeVisible();
    await expect(page.getByText('Add an OpenRouter API key before assigning this model.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Use OpenRouter Llama 70B' })).toBeDisabled();
  });

  test('model picker keeps sources and assignment actions usable at 375 pixels', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await setupProviderMocks(page);
    await page.goto('/settings?section=models&item=llm');

    await page.getByTestId('change-model-smart').click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const dialogWidth = await dialog.evaluate((element) => ({
      client: element.clientWidth,
      scroll: element.scrollWidth,
    }));
    expect(dialogWidth.scroll).toBe(dialogWidth.client);

    const sources = dialog.getByRole('navigation', { name: 'Model sources' });
    expect((await sources.boundingBox())?.height ?? Infinity).toBeLessThan(96);
    await page.getByRole('button', { name: 'OpenAI, 1 model' }).click();

    const useButton = page.getByRole('button', { name: 'Use GPT-4o' });
    await expect(useButton).toBeVisible();
    const useButtonBox = await useButton.boundingBox();
    expect((useButtonBox?.x ?? Infinity) + (useButtonBox?.width ?? Infinity)).toBeLessThanOrEqual(375);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
  });

  test('provider assignment links open the matching role and provider catalog', async ({ page }) => {
    await setupProviderMocks(page);
    await page.goto('/settings?section=models&item=providers');

    await page.getByRole('link', { name: 'Use for Main' }).click();
    await expect(page.getByRole('heading', { name: 'Choose a Main model' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'OpenAI, 1 model' })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByText('GPT-4o', { exact: true })).toBeVisible();
  });

  test('embedding route stays visible without a stored embedding override', async ({ page }) => {
    await setupProviderMocks(page);
    await page.goto('/settings?section=models&item=llm');

    await expect(page.getByRole('heading', { name: 'Embedding model' })).toBeVisible();
    await expect(page.getByText('qwen3-embedding:4b')).toBeVisible();
    await expect(page.getByText(/2,560 values per vector/)).toBeVisible();
    await expect(page.getByRole('link', { name: /embedding model migration guide/i })).toBeVisible();
  });

  test('provider setup remains usable on a narrow viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await setupProviderMocks(page);
    await page.goto('/settings?section=models&item=providers');

    await expect(page.getByRole('heading', { name: 'Providers & Routing', level: 2 })).toBeVisible({ timeout: 8000 });
    await page.getByRole('button', { name: 'Add cloud provider' }).click();
    await expect(page.getByRole('button', { name: /Custom OpenAI-compatible endpoint/ })).toBeVisible();
  });
});

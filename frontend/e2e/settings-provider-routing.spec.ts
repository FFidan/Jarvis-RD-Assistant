import { expect, test, type Page, type Route } from '@playwright/test';
import { installMockedApiDefaults } from './helpers/setup';

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
  },
];

const CONFIG = [
  { key: 'llm.openai.api_key', value: 'sk-****' },
  { key: 'llm.smart_model', value: 'qwen3:14b' },
  { key: 'llm.fast_model', value: 'qwen3:4b' },
];

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
      active: true, pulled: true, provider_key_present: false, fit: 'fits', can_assign: true, assign_blocker: null,
    },
    {
      id: 'openai/gpt-4o', name: 'GPT-4o', provider: 'openai', ollama_tag: null, roles: ['smart'],
      vram_gb: 0, disk_gb: 0, context_tokens: 128000, license: 'Commercial', tier: 0,
      description: 'Cloud model.', notes: '', last_reviewed: '2026-07-06', status: 'cloud_required',
      active: false, pulled: false, provider_key_present: true, fit: 'cloud', can_assign: true, assign_blocker: null,
    },
    {
      id: 'openrouter/meta-llama/llama-3.1-70b-instruct', name: 'OpenRouter Llama 70B', provider: 'openrouter', ollama_tag: null, roles: ['smart'],
      vram_gb: 0, disk_gb: 0, context_tokens: 128000, license: 'Commercial', tier: 0,
      description: 'Router model.', notes: '', last_reviewed: '2026-07-06', status: 'cloud_required',
      active: false, pulled: false, provider_key_present: false, fit: 'cloud', can_assign: false,
      assign_blocker: 'Add an OpenRouter API key before assigning this model.',
    },
  ],
  recommendations: {},
};

async function seedAdminSession(page: Page) {
  const apiKey = process.env.JARVIS_API_KEY ?? 'dev';
  await page.addInitScript((key: string) => {
    const state = {
      state: {
        isAuthenticated: true,
        authTime: Date.now(),
        apiKey: key,
        user: { id: 1, email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
    window.localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  }, apiKey);
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
    await expect(page.getByRole('button', { name: 'OpenAI Configured, not tested' })).toBeVisible();
    await expect(page.getByText('Configured, not tested')).toBeVisible();

    await page.getByRole('button', { name: 'Add cloud provider' }).click();
    await expect(page.getByText('Recommended routers')).toBeVisible();
    await expect(page.getByRole('button', { name: /OpenRouter/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Custom OpenAI-compatible endpoint/ })).toBeVisible();

    await page.getByRole('button', { name: /Custom OpenAI-compatible endpoint/ }).click();
    await expect(page.getByText('Trusted self-hosted or institutional gateways.')).toBeVisible();
    await expect(page.getByLabel('API key')).toBeVisible();
    await expect(page.getByLabel('Base URL')).toBeVisible();
    await expect(page.getByText(/Admin-wide setting/)).toBeVisible();
  });

  test('model selector keeps local first and shows missing-key cloud routes disabled', async ({ page }) => {
    await setupProviderMocks(page);
    await page.goto('/settings?section=models&item=llm');

    await expect(page.getByRole('heading', { name: 'AI models', level: 2 })).toBeVisible({ timeout: 8000 });
    await page.getByRole('combobox').first().click();
    await expect(page.getByText('Ollama (default)').first()).toBeVisible();
    await expect(page.getByText('OpenAI').first()).toBeVisible();
    await expect(page.getByText('GPT-4o', { exact: true })).toBeVisible();
    await expect(page.getByText('OpenRouter').first()).toBeVisible();
    await expect(page.getByText('OpenRouter Llama 70B')).toBeVisible();
    await expect(page.getByText('Add an OpenRouter API key before assigning this model.')).toBeVisible();
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

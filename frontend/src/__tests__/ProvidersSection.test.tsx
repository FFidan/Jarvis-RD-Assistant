import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ProvidersSection } from '@/components/settings/ProvidersSection';
import type {
  ModelCatalogEntry,
  ProviderMetadata,
  ProviderModelListStatus,
  SystemModelsResponse,
} from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
    fetchProviderAccount: vi.fn(),
    fetchSystemModels: vi.fn(),
    listProviders: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: '', value: null }),
    testProvider: vi.fn(),
    listJobs: vi.fn().mockResolvedValue([]),
    cancelJob: vi.fn(),
    getJob: vi.fn(),
  };
});

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      logout: vi.fn(),
    })),
  },
}));

const {
  fetchConfig,
  fetchProviderAccount,
  fetchSystemModels,
  listProviders,
  setConfig,
  testProvider,
} = await import('@/lib/api');
const { toast } = await import('sonner');

const EMPTY_SYSTEM_MODELS: SystemModelsResponse = {
  status: 'ok',
  installed: [],
  hardware: {},
  current: {},
  issues: {},
  catalog: [],
  recommendations: {},
  hardware_recommendation: {
    vram_mb: null,
    bucket: 'CPU_ONLY',
    summary: 'CPU-only test fixture',
    aliases: [],
  },
  delivery: {},
  routing: {},
  consistent: true,
  provider_lists: {},
};

function modelEntry(provider: string): ModelCatalogEntry {
  return {
    id: `${provider}/test-model`,
    name: 'Test model',
    provider,
    ollama_tag: null,
    roles: ['smart'],
    vram_gb: 0,
    disk_gb: 0,
    context_tokens: 8192,
    license: 'test',
    tier: 0,
    description: 'Test fixture',
    notes: '',
    last_reviewed: '2026-08-09',
    embedding_dimension: null,
    phase: 'test',
    assignable: true,
    min_vram_gb_at_default_ctx: null,
    kv_cache_bytes_per_token: null,
    default_num_ctx: null,
    max_num_ctx: null,
    supports_thinking: false,
    active: false,
    pulled: false,
    provider_key_present: true,
    fit: 'available',
    status: 'cloud_active',
    can_assign: true,
    assign_blocker: null,
    fit_detail: {
      default: 'cloud',
      at_num_ctx: 8192,
      required_vram_gb: null,
      base_vram_gb: null,
      base_num_ctx: 8192,
      default_num_ctx: 8192,
      max_num_ctx: 8192,
      kv_cache_bytes_per_token: null,
    },
  };
}

function providerListStatus(
  fields: Pick<ProviderModelListStatus, 'fetched_at' | 'error'>,
): ProviderModelListStatus {
  return {
    model_count: 0,
    fetched_at: fields.fetched_at,
    error: fields.error,
    truncated: false,
    excluded: {},
  };
}

function systemModels(
  fields: Pick<SystemModelsResponse, 'catalog' | 'provider_lists'>,
): SystemModelsResponse {
  return { ...EMPTY_SYSTEM_MODELS, ...fields };
}

const PROVIDERS: ProviderMetadata[] = [
  {
    id: 'anthropic',
    display_name: 'Anthropic Claude',
    kind: 'direct',
    api_key_config_key: 'llm.anthropic.api_key',
    base_url_config_key: null,
    assignment_prefix: 'anthropic/',
    litellm_prefix: 'anthropic/',
    privacy_boundary: 'direct_provider',
    best_for: 'Careful long-context synthesis and writing.',
    data_note: 'Selected prompts and source excerpts are sent to Anthropic when assigned.',
    configured: true,
    base_url_configured: false,
    supports_assignment: true,
    dashboard_url: 'https://console.anthropic.com/',
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
    best_for: 'Trying many hosted models through one router account.',
    data_note: 'Requests pass through OpenRouter and then the selected upstream provider.',
    configured: false,
    base_url_configured: false,
    supports_assignment: true,
    dashboard_url: 'https://openrouter.ai/dashboard/api-keys',
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
    best_for: 'Self-hosted vLLM, institutional gateways, or compatible endpoints.',
    data_note: 'Requests are sent to the configured endpoint. Verify its operator and logs.',
    configured: false,
    base_url_configured: false,
    supports_assignment: true,
    dashboard_url: null,
    account_capability: 'unavailable',
  },
];

const MASKED_CONFIG = [{ key: 'llm.anthropic.api_key', value: '****1234' }];

function renderSection(initialProviderId?: string) {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <ProvidersSection initialProviderId={initialProviderId} />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('ProvidersSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue(MASKED_CONFIG);
    vi.mocked(fetchProviderAccount).mockResolvedValue({
      provider: 'openrouter',
      capability: 'current_key',
      data: {},
      error_code: null,
    });
    vi.mocked(listProviders).mockResolvedValue(PROVIDERS);
    vi.mocked(fetchSystemModels).mockResolvedValue(EMPTY_SYSTEM_MODELS);
  });

  it('shows connected providers first without exposing every provider input', async () => {
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('Providers & Routing')).toBeInTheDocument();
    });

    expect(screen.getAllByText('Configured; not checked yet')).not.toHaveLength(0);
    expect(screen.getAllByText('Anthropic Claude')).not.toHaveLength(0);
    expect(screen.getByRole('button', { name: /add cloud provider/i })).toBeInTheDocument();
    expect(screen.queryByLabelText('Base URL')).not.toBeInTheDocument();
  });

  it('opens an add-provider chooser and selects OpenRouter', async () => {
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /add cloud provider/i }));
    await user.click(screen.getByRole('button', { name: /OpenRouter/i }));

    expect(screen.getByText('Trying many hosted models through one router account.')).toBeInTheDocument();
    expect(document.getElementById('provider-key-openrouter')).toBeInTheDocument();
  });

  it('links Quick and Main assignment back to the single AI model plane', async () => {
    renderSection();

    expect(await screen.findByRole('link', { name: 'Use for Quick' })).toHaveAttribute(
      'href',
      '/settings?section=models&item=llm&role=fast&provider=anthropic',
    );
    expect(screen.getByRole('link', { name: 'Use for Main' })).toHaveAttribute(
      'href',
      '/settings?section=models&item=llm&role=smart&provider=anthropic',
    );
  });

  it('opens an explicitly linked provider instead of falling back to the first configured one', async () => {
    renderSection('openrouter');

    expect(await screen.findByText('Trying many hosted models through one router account.')).toBeInTheDocument();
    expect(document.getElementById('provider-key-openrouter')).toBeInTheDocument();
  });

  it('renders only the supported current-key account snapshot fields', async () => {
    vi.mocked(listProviders).mockResolvedValue(
      PROVIDERS.map((provider) =>
        provider.id === 'openrouter' ? { ...provider, configured: true } : provider,
      ),
    );
    vi.mocked(fetchProviderAccount).mockResolvedValue({
      provider: 'openrouter',
      capability: 'current_key',
      data: {
        is_free_tier: false,
        usage_monthly: 0.18,
        limit_remaining: 4.82,
      },
      error_code: null,
    });
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /OpenRouter/ }));
    expect(await screen.findByText('Current-key details')).toBeInTheDocument();
    expect(screen.getByText('Connection')).toBeInTheDocument();
    expect(screen.getByText('Models')).toBeInTheDocument();
    expect(screen.getByText('Account data')).toBeInTheDocument();
    expect(screen.getByText('Provider dashboard')).toBeInTheDocument();
    expect(screen.getByText('Usage this month')).toBeInTheDocument();
    expect(screen.getByText('Limit remaining')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Open provider dashboard/ })).toHaveAttribute(
      'href',
      'https://openrouter.ai/dashboard/api-keys',
    );
    expect(screen.queryByText(/creator|workspace|hash/i)).not.toBeInTheDocument();
  });

  it('replaces a key only after explicit save and lets the user cancel', async () => {
    const user = userEvent.setup();
    renderSection();

    expect(await screen.findByDisplayValue('****1234')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Replace key' }));
    const input = screen.getByLabelText('API key') as HTMLInputElement;
    await user.type(input, 'cancelled-key-value');
    await user.tab();
    expect(vi.mocked(setConfig)).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(vi.mocked(setConfig)).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Replace key' }));
    await user.type(screen.getByLabelText('API key'), 'replacement-key-value');
    await user.click(screen.getByRole('button', { name: 'Save key' }));

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith(
        'llm.anthropic.api_key',
        'replacement-key-value',
      );
    });
  });

  it('saves custom endpoint base URL through the provider registry config key', async () => {
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /add cloud provider/i }));
    await user.click(screen.getByRole('button', { name: /Custom OpenAI-compatible endpoint/i }));

    await user.click(screen.getByRole('button', { name: 'Add endpoint' }));
    const baseUrl = screen.getByLabelText('Base URL');
    await user.type(baseUrl, 'http://127.0.0.1:8000/v1');
    await user.tab();
    expect(vi.mocked(setConfig)).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Save endpoint' }));

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith(
        'llm.providers.custom_openai_compatible.base_url',
        'http://127.0.0.1:8000/v1',
      );
    });
  });

  it('keeps an API key draft visible when saving fails', async () => {
    vi.mocked(setConfig).mockRejectedValueOnce(new Error('Save failed'));
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Replace key' }));
    const input = screen.getByLabelText('API key') as HTMLInputElement;
    await user.type(input, 'unsaved-key-value');
    await user.click(screen.getByRole('button', { name: 'Save key' }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Save failed');
    });
    expect(input.value).toBe('unsaved-key-value');
  });

  it('keeps a base URL draft visible when saving fails', async () => {
    vi.mocked(setConfig).mockRejectedValueOnce(new Error('Invalid base URL'));
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /add cloud provider/i }));
    await user.click(screen.getByRole('button', { name: /Custom OpenAI-compatible endpoint/i }));

    await user.click(screen.getByRole('button', { name: 'Add endpoint' }));
    const baseUrl = screen.getByLabelText('Base URL') as HTMLInputElement;
    await user.type(baseUrl, 'https://gateway.example.invalid/v1');
    await user.click(screen.getByRole('button', { name: 'Save endpoint' }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Invalid base URL');
    });
    expect(baseUrl.value).toBe('https://gateway.example.invalid/v1');
  });

  it('tests the selected provider and reports success', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: true, error: null });
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Test now' }));

    await waitFor(() => {
      expect(vi.mocked(testProvider)).toHaveBeenCalledWith('anthropic');
      expect(vi.mocked(toast.success)).toHaveBeenCalledWith('Anthropic Claude connection passed');
    });
  });

  it('shows provider metadata load failures', async () => {
    vi.mocked(listProviders).mockRejectedValue(new Error('Admin access required'));

    renderSection();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load provider settings. Admin access required',
    );
  });

  it('shows provider test failures without clearing the stored key', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: false, error: 'Invalid API key' });
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Test now' }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Invalid API key');
    });
    expect(screen.getAllByText('Configured; not checked yet')).not.toHaveLength(0);
    expect(screen.getByRole('alert')).toHaveTextContent('Connection test failed: Invalid API key');
  });

  it('uses refreshed provider-list truth after a failed connection test', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: false, error: 'Temporary test failure' });
    vi.mocked(fetchSystemModels)
      .mockResolvedValueOnce(EMPTY_SYSTEM_MODELS)
      .mockResolvedValueOnce(systemModels({
        catalog: [modelEntry('anthropic')],
        provider_lists: {
          anthropic: providerListStatus({
            fetched_at: new Date().toISOString(),
            error: null,
          }),
        },
      }));
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Test now' }));

    expect(await screen.findAllByText(/Connected · checked/)).not.toHaveLength(0);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Connection test failed: Temporary test failure',
    );
    expect(screen.queryByText('Configured; connection needs attention')).not.toBeInTheDocument();
  });

  it.each([
    ['provider_authentication_failed', 'Provider rejected this key'],
    ['provider_payment_required', 'Provider account needs billing attention'],
    ['provider_rate_limited', 'Provider rate limit reached'],
    ['provider_unavailable', 'Provider service is unavailable'],
    ['provider_request_timed_out', 'Provider did not respond in time'],
  ])('explains account failure %s without provider response details', async (errorCode, copy) => {
    vi.mocked(listProviders).mockResolvedValue(
      PROVIDERS.map((provider) =>
        provider.id === 'openrouter' ? { ...provider, configured: true } : provider,
      ),
    );
    vi.mocked(fetchProviderAccount).mockResolvedValue({
      provider: 'openrouter',
      capability: 'current_key',
      data: {},
      error_code: errorCode,
    });

    renderSection('openrouter');

    expect(await screen.findByText(copy)).toBeInTheDocument();
    expect(screen.queryByText('Temporarily unavailable')).not.toBeInTheDocument();
  });

  it('shows model count and fetch staleness for a connected provider with a live list', async () => {
    vi.mocked(fetchSystemModels).mockResolvedValue(systemModels({
      catalog: [
        modelEntry('anthropic'),
        modelEntry('anthropic'),
      ],
      provider_lists: {
        anthropic: providerListStatus({ fetched_at: new Date().toISOString(), error: null }),
      },
    }));
    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/2 models available/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Checked/)).toBeInTheDocument();
  });

  it('shows the unavailable text for a connected provider whose live fetch errored', async () => {
    vi.mocked(fetchSystemModels).mockResolvedValue(systemModels({
      catalog: [],
      provider_lists: {
        anthropic: providerListStatus({
          fetched_at: '2026-01-01T00:00:00Z',
          error: 'provider request failed',
        }),
      },
    }));
    renderSection();

    await waitFor(() => {
      expect(
        screen.getByText("Catalog unavailable — JARVIS could not refresh this provider's models"),
      ).toBeInTheDocument();
    });
  });

  it('shows both tested-connectivity and zero-models availability for the same tile', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: true, error: null });
    vi.mocked(fetchSystemModels).mockResolvedValue(systemModels({
      catalog: [],
      provider_lists: {
        anthropic: providerListStatus({ fetched_at: null, error: 'provider request failed' }),
      },
    }));
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: 'Test now' }));

    await waitFor(() => {
      expect(screen.getAllByText('Configured; connection needs attention')).not.toHaveLength(0);
    });
    expect(
      screen.getByText("Catalog unavailable — JARVIS could not refresh this provider's models"),
    ).toBeInTheDocument();
  });

  it('does not blame connectivity when the fetch succeeded but offered nothing usable', async () => {
    // Every model a provider lists can be excluded or already bundled. Telling the
    // operator to fix a working connection sends them after a problem they do not have.
    vi.mocked(fetchSystemModels).mockResolvedValue(systemModels({
      catalog: [],
      provider_lists: {
        anthropic: providerListStatus({ fetched_at: '2026-01-01T00:00:00Z', error: null }),
      },
    }));
    renderSection();

    expect(
      await screen.findByText('Catalog checked — no compatible models were returned'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Catalog unavailable — JARVIS could not refresh this provider's models"),
    ).not.toBeInTheDocument();
  });

  it('renders a tile and its availability line for a base-URL-only provider with no key', async () => {
    vi.mocked(listProviders).mockResolvedValue([
      ...PROVIDERS,
      {
        id: 'custom_openai_compatible2',
        display_name: 'Second Custom Endpoint',
        kind: 'self_hosted',
        api_key_config_key: 'llm.providers.custom_openai_compatible2.api_key',
        base_url_config_key: 'llm.providers.custom_openai_compatible2.base_url',
        assignment_prefix: 'custom_openai2/',
        litellm_prefix: 'openai/',
        privacy_boundary: 'self_hosted',
        best_for: 'A second self-hosted endpoint.',
        data_note: 'Requests are sent to the configured endpoint.',
        configured: false,
        base_url_configured: true,
        supports_assignment: true,
        dashboard_url: null,
        account_capability: 'unavailable',
      },
    ]);
    vi.mocked(fetchSystemModels).mockResolvedValue(systemModels({
      catalog: [modelEntry('custom_openai_compatible2')],
      provider_lists: {
        custom_openai_compatible2: providerListStatus({
          fetched_at: '2026-01-01T00:00:00Z',
          error: null,
        }),
      },
    }));
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('Second Custom Endpoint')).toBeInTheDocument();
    });
    expect(screen.getByText(/1 model available/)).toBeInTheDocument();
  });
});

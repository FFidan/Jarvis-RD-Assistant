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

const { fetchConfig, fetchSystemModels, listProviders, setConfig, testProvider } = await import('@/lib/api');
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
  },
];

const MASKED_CONFIG = [{ key: 'llm.anthropic.api_key', value: '****1234' }];

function renderSection() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <ProvidersSection />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('ProvidersSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue(MASKED_CONFIG);
    vi.mocked(listProviders).mockResolvedValue(PROVIDERS);
    vi.mocked(fetchSystemModels).mockResolvedValue(EMPTY_SYSTEM_MODELS);
  });

  it('shows connected providers first without exposing every provider input', async () => {
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('Providers & Routing')).toBeInTheDocument();
    });

    expect(screen.getByText('Connected')).toBeInTheDocument();
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

  it('saves changed API keys on blur and preserves blank drafts as no-op', async () => {
    const user = userEvent.setup();
    renderSection();

    const input = (await screen.findByLabelText('API key')) as HTMLInputElement;
    expect(input.value).toBe('****1234');
    await user.clear(input);
    await user.tab();
    expect(vi.mocked(setConfig)).not.toHaveBeenCalled();

    await user.click(input);
    await user.clear(input);
    await user.type(input, 'sk-ant-new');
    await user.tab();

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('llm.anthropic.api_key', 'sk-ant-new');
    });
  });

  it('saves custom endpoint base URL through the provider registry config key', async () => {
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /add cloud provider/i }));
    await user.click(screen.getByRole('button', { name: /Custom OpenAI-compatible endpoint/i }));

    const baseUrl = screen.getByLabelText('Base URL');
    await user.type(baseUrl, 'http://127.0.0.1:8000/v1');
    await user.tab();

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

    const input = (await screen.findByLabelText('API key')) as HTMLInputElement;
    await user.clear(input);
    await user.type(input, 'sk-ant-unsaved');
    await user.tab();

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Save failed');
    });
    expect(input.value).toBe('sk-ant-unsaved');
  });

  it('keeps a base URL draft visible when saving fails', async () => {
    vi.mocked(setConfig).mockRejectedValueOnce(new Error('Invalid base URL'));
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /add cloud provider/i }));
    await user.click(screen.getByRole('button', { name: /Custom OpenAI-compatible endpoint/i }));

    const baseUrl = screen.getByLabelText('Base URL') as HTMLInputElement;
    await user.type(baseUrl, 'https://gateway.example.invalid/v1');
    await user.tab();

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Invalid base URL');
    });
    expect(baseUrl.value).toBe('https://gateway.example.invalid/v1');
  });

  it('tests the selected provider and reports success', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: true, error: null });
    const user = userEvent.setup();
    renderSection();

    await user.click(await screen.findByRole('button', { name: /Test Anthropic Claude connection/i }));

    await waitFor(() => {
      expect(vi.mocked(testProvider)).toHaveBeenCalledWith('anthropic');
      expect(vi.mocked(toast.success)).toHaveBeenCalledWith('Anthropic Claude connection OK');
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

    await user.click(await screen.findByRole('button', { name: /Test Anthropic Claude connection/i }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Invalid API key');
    });
    expect(screen.getByText('Configured, degraded: Invalid API key')).toBeInTheDocument();
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
    expect(screen.getByText(/Fetched/)).toBeInTheDocument();
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
        screen.getByText("No models available yet — JARVIS could not fetch this provider's model list"),
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

    await user.click(await screen.findByRole('button', { name: /Test Anthropic Claude connection/i }));

    await waitFor(() => {
      expect(screen.getByText('Configured and tested')).toBeInTheDocument();
    });
    expect(
      screen.getByText("No models available yet — JARVIS could not fetch this provider's model list"),
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
      await screen.findByText('No models available yet — this provider offered none JARVIS can use'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("No models available yet — JARVIS could not fetch this provider's model list"),
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

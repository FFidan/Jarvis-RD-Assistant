import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ProvidersSection } from '@/components/settings/ProvidersSection';
import type { ProviderMetadata } from '@/lib/api';

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
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
      getApiKey: vi.fn(() => 'test-key'),
      logout: vi.fn(),
    })),
  },
}));

const { fetchConfig, listProviders, setConfig, testProvider } = await import('@/lib/api');
const { toast } = await import('sonner');

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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ProvidersSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ProvidersSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue(MASKED_CONFIG);
    vi.mocked(listProviders).mockResolvedValue(PROVIDERS);
  });

  it('shows connected providers first without exposing every provider input', async () => {
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('Providers and routing')).toBeInTheDocument();
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
});

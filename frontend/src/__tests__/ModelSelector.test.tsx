import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelSelector } from '@/components/shared/ModelSelector';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const modelApiMocks = vi.hoisted(() => ({
  fetchSystemModels: vi.fn(),
  apiFetchVoid: vi.fn(),
}));

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  return createApiMock({
    fetchSystemModels: modelApiMocks.fetchSystemModels,
    apiFetchVoid: modelApiMocks.apiFetchVoid,
  });
});

const localSmart = {
  id: 'qwen3:14b', name: 'Qwen3 14B', provider: 'ollama', ollama_tag: 'qwen3:14b',
  roles: ['smart'], vram_gb: 9.5, disk_gb: 9.2, context_tokens: 32768,
  license: 'Apache 2.0', tier: 2, description: 'Strong local reasoning.', notes: '',
  last_reviewed: '2026-05-03', status: 'active', active: true, pulled: true,
  provider_key_present: false, fit: 'fits', can_assign: true, assign_blocker: null,
};
const downloadableFast = {
  id: 'qwen3:4b', name: 'Qwen3 4B', provider: 'ollama', ollama_tag: 'qwen3:4b',
  roles: ['fast'], vram_gb: 3.5, disk_gb: 2.5, context_tokens: 32768,
  license: 'Apache 2.0', tier: 1, description: 'Fast local model.', notes: '',
  last_reviewed: '2026-05-03', status: 'downloadable', active: false, pulled: false,
  provider_key_present: false, fit: 'fits', can_assign: false,
  assign_blocker: 'Pull this model before assigning it.',
};
const downloadableSmart = {
  ...downloadableFast, id: 'qwen3:8b', name: 'Qwen3 8B', ollama_tag: 'qwen3:8b',
  roles: ['smart'], vram_gb: 5.5, disk_gb: 4.9,
};
const inactiveEmbedding = {
  ...localSmart, id: 'qwen3-embedding:0.6b', name: 'Qwen3 Embedding 0.6B',
  ollama_tag: 'qwen3-embedding:0.6b', roles: ['embed'], vram_gb: 1.2, disk_gb: 0.6,
  status: 'pulled', active: false, pulled: true,
};
const unfitSmart = {
  ...localSmart, id: 'qwen3:30b-a3b', name: 'Qwen3 30B-A3B', ollama_tag: 'qwen3:30b-a3b',
  status: 'unfit', active: false, pulled: false, can_assign: false,
  assign_blocker: 'Requires more VRAM.', fit_detail: { default: 'unfit' },
};
const openAiSmart = {
  id: 'openai/gpt-4o', name: 'GPT-4o', provider: 'openai', ollama_tag: null,
  roles: ['smart'], vram_gb: 0, disk_gb: 0, context_tokens: 128000,
  license: 'Commercial', tier: 0, description: 'Cloud reasoning model.', notes: '',
  last_reviewed: '2026-05-03', status: 'cloud', active: false, pulled: false,
  provider_key_present: true, fit: 'cloud', can_assign: true, assign_blocker: null,
};
const anthropicBlocked = {
  ...openAiSmart, id: 'anthropic/claude-haiku-4-5', name: 'Claude Haiku 4.5',
  provider: 'anthropic', provider_key_present: false, can_assign: false,
  assign_blocker: 'Add an Anthropic API key before assigning this model.',
};
const openRouterFree = {
  ...openAiSmart, id: 'openrouter/inclusionai/ling-3.0-tiny:free', name: 'Ling 3.0 Tiny',
  provider: 'openrouter', input_price_per_million: '0', output_price_per_million: '0',
  price_source: 'openrouter',
};
const openRouterPaid = {
  ...openAiSmart, id: 'openrouter/anthropic/claude-sonnet-4', name: 'Claude Sonnet 4',
  provider: 'openrouter', input_price_per_million: '3', output_price_per_million: '15',
  price_source: 'openrouter',
};

const defaultModels = {
  status: 'ok', installed: [], hardware: { ollama_running: 1 },
  current: { smart_model: 'qwen3:14b', fast_model: 'qwen3:4b' },
  issues: {}, catalog: [localSmart, downloadableFast, unfitSmart, openAiSmart, anthropicBlocked],
  recommendations: {}, reviewed_choices: {}, provider_lists: {}, routing: {},
};

function renderComponent(props: Partial<React.ComponentProps<typeof ModelSelector>> = {}) {
  const queryClient = createTestQueryClient();
  const onChange = vi.fn();
  renderWithProviders(
    <ModelSelector value="" onChange={onChange} configKey="llm.smart_model" {...props} />,
    { queryClient },
  );
  return { onChange };
}

async function openPicker(role: 'smart' | 'fast' = 'smart') {
  const user = userEvent.setup();
  await user.click(await screen.findByTestId(`change-model-${role}`));
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
  return user;
}

describe('ModelSelector model picker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    modelApiMocks.fetchSystemModels.mockResolvedValue(defaultModels);
    modelApiMocks.apiFetchVoid.mockResolvedValue(undefined);
  });

  it('opens a role-specific dialog and keeps incompatible models out', async () => {
    renderComponent();
    await openPicker();
    expect(screen.getByText('Choose a Main model')).toBeInTheDocument();
    expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
    expect(screen.getByTestId('model-row-qwen3:14b')).toHaveTextContent('No provider charge');
    expect(screen.getByRole('button', { name: 'Qwen3 14B is current' })).toBeDisabled();
    expect(screen.queryByText('Qwen3 4B')).not.toBeInTheDocument();
  });

  it('assigns an available cloud model and closes the dialog', async () => {
    const { onChange } = renderComponent();
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: /OpenAI/ }));
    await user.click(screen.getByRole('button', { name: 'Use GPT-4o' }));
    expect(onChange).toHaveBeenCalledWith('openai/gpt-4o');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('opens directly on a provider requested by the provider workspace', async () => {
    renderComponent({ initialSource: 'openai', defaultOpen: true });

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /OpenAI/ })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('GPT-4o')).toBeInTheDocument();
    expect(screen.queryByText('Qwen3 14B')).not.toBeInTheDocument();
  });

  it('shows backend assignment blockers and never enables the blocked row', async () => {
    renderComponent();
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: /Anthropic/ }));
    const row = screen.getByTestId('model-row-anthropic/claude-haiku-4-5');
    expect(row).toHaveTextContent('Add an Anthropic API key before assigning this model.');
    expect(within(row).getByRole('button', { name: 'Use Claude Haiku 4.5' })).toBeDisabled();
  });

  it('shows exact OpenRouter prices and filters free models', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [...defaultModels.catalog, openRouterPaid, openRouterFree],
      recommendations: { smart: [{ id: openRouterFree.id }] },
    });
    renderComponent();
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: /OpenRouter/ }));
    expect(screen.getByText('$3 input / $15 output per 1M tokens')).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Price'), 'free');
    expect(screen.getByText('Ling 3.0 Tiny')).toBeInTheDocument();
    expect(screen.queryByText('Claude Sonnet 4')).not.toBeInTheDocument();
    expect(screen.getByTestId(`model-row-${openRouterFree.id}`)).toHaveTextContent('Free');
  });

  it('keeps reviewed choices distinct from automatic recommendations', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [...defaultModels.catalog, openRouterPaid, openRouterFree],
      recommendations: { smart: [openRouterPaid] },
      reviewed_choices: { smart: [openRouterFree] },
    });
    renderComponent();
    const user = await openPicker();

    expect(screen.getByRole('button', { name: 'Reviewed choices, 1 model' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(screen.getByText('Ling 3.0 Tiny')).toBeInTheDocument();
    expect(screen.queryByText('Claude Sonnet 4')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /OpenRouter/ }));
    expect(screen.getByText('Claude Sonnet 4')).toBeInTheDocument();
  });

  it('shows capabilities, lifecycle, and metadata provenance without guessing', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [{
        ...openRouterPaid,
        capabilities: ['text_input', 'tool_use'],
        lifecycle: 'active',
        field_sources: {
          capabilities: { kind: 'api_reported', fetched_at: '2026-08-11T08:00:00Z' },
        },
      }],
    });
    renderComponent({ value: openRouterPaid.id });
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: /OpenRouter/ }));

    const row = screen.getByTestId(`model-row-${openRouterPaid.id}`);
    expect(row).toHaveTextContent('Capabilities: text input, tool use');
    expect(row).toHaveTextContent('Lifecycle: active');
    expect(row).toHaveTextContent(/Provider metadata · fetched/);
  });

  it('keeps a 500-model provider catalogue usable and truthfully counted', async () => {
    const largeCatalog = Array.from({ length: 500 }, (_, index) => ({
      ...openRouterPaid,
      id: `openrouter/test/model-${index}`,
      name: `Model ${index}`,
    }));
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: largeCatalog,
    });
    renderComponent({ value: largeCatalog[0]?.id ?? '' });
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: 'OpenRouter, 500 models' }));

    expect(screen.getByText('500 matching models')).toBeInTheDocument();
    await user.type(screen.getByRole('searchbox', { name: 'Search models' }), 'Model 499');
    expect(screen.getByText('Model 499')).toBeInTheDocument();
    expect(screen.getByText('1 matching model')).toBeInTheDocument();
  });

  it('searches only within the selected provider catalog', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [...defaultModels.catalog, openRouterPaid, openRouterFree],
    });
    renderComponent();
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: /OpenRouter/ }));
    await user.type(screen.getByRole('searchbox', { name: 'Search models' }), 'sonnet');
    expect(screen.getByText('Claude Sonnet 4')).toBeInTheDocument();
    expect(screen.queryByText('Ling 3.0 Tiny')).not.toBeInTheDocument();
  });

  it('keeps unknown prices last when sorting by input price', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [openRouterPaid, { ...openRouterFree, input_price_per_million: undefined, output_price_per_million: undefined }],
    });
    renderComponent({ value: openRouterPaid.id });
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: /OpenRouter/ }));
    await user.selectOptions(screen.getByLabelText('Sort models'), 'input-price');
    const rows = screen.getAllByTestId(/model-row-/);
    expect(rows[0]).toHaveTextContent('Claude Sonnet 4');
    expect(rows[1]).toHaveTextContent('Ling 3.0 Tiny');
  });

  it('shows a clear empty state for a role with no compatible models', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({ ...defaultModels, catalog: [] });
    renderComponent();
    expect(await screen.findByText('No compatible models are available for this role.')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByTestId('change-model-smart')).not.toBeInTheDocument();
    });
  });

  it('reports model API failures without pretending the catalog is empty', async () => {
    modelApiMocks.fetchSystemModels.mockRejectedValue(new Error('offline'));
    renderComponent();
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load models. Check the API and model service status.');
  });

  it('surfaces a degraded backend issue instead of guessing at an empty catalog', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      status: 'degraded',
      catalog: [],
      issues: { installed: 'Could not load installed Ollama models.' },
    });
    renderComponent();
    expect(await screen.findByText('Could not load installed Ollama models.')).toBeInTheDocument();
    expect(screen.queryByText('No compatible models are available for this role.')).not.toBeInTheDocument();
  });

  it('surfaces saved-versus-served route divergence', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      routing: { smart: 'openai/gpt-4o' },
    });
    renderComponent({ value: 'qwen3:14b' });
    expect(await screen.findByTestId('routing-diverged-smart')).toHaveTextContent(
      'You selected "qwen3:14b" but the model service is currently using "openai/gpt-4o".',
    );
  });

  it.each([
    ['a matching runtime route', { smart: 'qwen3:14b' }],
    ['no runtime route', undefined],
  ])('does not report divergence for %s', async (_case, routing) => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      routing,
    });
    renderComponent({ value: 'qwen3:14b' });
    await screen.findByTestId('change-model-smart');
    expect(screen.queryByTestId('routing-diverged-smart')).not.toBeInTheDocument();
  });

  it('matches a selected local model when its stored value has a latest suffix', async () => {
    renderComponent({ value: 'qwen3:14b:latest' });
    await openPicker();
    expect(screen.getByRole('button', { name: 'Qwen3 14B is current' })).toBeDisabled();
  });

  it.each([
    ['failed', { fetched_at: null, error: 'provider request failed', truncated: false }, 'Live catalog unavailable; built-in entries may still be shown.'],
    ['empty', { fetched_at: '2026-08-01T00:00:00Z', error: null, truncated: false }, '0 matching models'],
  ])('distinguishes a %s provider catalog with no role-compatible models', async (_case, providerStatus, statusText) => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      provider_lists: { deepseek: providerStatus },
    });
    renderComponent();
    const user = await openPicker();
    await user.click(screen.getByRole('button', { name: /DeepSeek/ }));
    expect(screen.getByText(statusText)).toBeInTheDocument();
    expect(screen.getByText('No models match these filters.')).toBeInTheDocument();
  });
});

describe('ModelSelector local lifecycle controls', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    modelApiMocks.fetchSystemModels.mockResolvedValue(defaultModels);
    modelApiMocks.apiFetchVoid.mockResolvedValue(undefined);
  });

  it('keeps install controls behind the local-route disclosure', async () => {
    renderComponent({ configKey: 'llm.fast_model', value: 'qwen3:4b' });
    const user = userEvent.setup();
    const disclosure = await screen.findByRole('button', { name: 'Install & manage local models' });
    expect(screen.queryByRole('button', { name: 'Pull model Qwen3 4B' })).not.toBeInTheDocument();
    await user.click(disclosure);
    expect(screen.getByRole('button', { name: 'Pull model Qwen3 4B' })).toBeInTheDocument();
  });

  it('confirms before pulling a model', async () => {
    renderComponent({ configKey: 'llm.fast_model', value: 'qwen3:4b' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Install & manage local models' }));
    await user.click(screen.getByRole('button', { name: 'Pull model Qwen3 4B' }));
    await user.click(await screen.findByRole('button', { name: 'Pull' }));
    await waitFor(() => {
      expect(modelApiMocks.apiFetchVoid).toHaveBeenCalledWith(
        '/api/system/models/qwen3%3A4b/pull',
        { method: 'POST' },
      );
    });
  });

  it('does not assign a downloadable model while its pull is pending', async () => {
    let finishPull: (() => void) | undefined;
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [...defaultModels.catalog, downloadableSmart],
    });
    modelApiMocks.apiFetchVoid.mockImplementation(
      () => new Promise<void>((resolve) => { finishPull = resolve; }),
    );
    const { onChange } = renderComponent({ value: localSmart.id });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Install & manage local models' }));
    await user.click(screen.getByRole('button', { name: 'Pull model Qwen3 8B' }));
    await user.click(await screen.findByRole('button', { name: 'Pull' }));
    await waitFor(() => expect(modelApiMocks.apiFetchVoid).toHaveBeenCalled());
    expect(onChange).not.toHaveBeenCalled();
    finishPull?.();
  });

  it('omits pull controls for a downloadable model that fails the detailed fit check', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [
        ...defaultModels.catalog,
        { ...downloadableSmart, fit_detail: { default: 'unfit' } },
      ],
    });
    renderComponent({ value: localSmart.id });
    await openPicker();
    const row = screen.getByTestId('model-row-qwen3:8b');
    expect(within(row).getByRole('button', { name: 'Use Qwen3 8B' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Install & manage local models' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pull model Qwen3 8B' })).not.toBeInTheDocument();
  });

  it('requires confirmation before deleting an inactive pulled local model', async () => {
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { embed_model: inactiveEmbedding.id },
      catalog: [...defaultModels.catalog, inactiveEmbedding],
    });
    renderComponent({ value: inactiveEmbedding.id, configKey: 'llm.embed_model' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Install & manage local models' }));
    await user.click(screen.getByRole('button', { name: 'Delete model Qwen3 Embedding 0.6B' }));
    expect(modelApiMocks.apiFetchVoid).not.toHaveBeenCalled();
    expect(screen.getByText(/frees approximately 0.6 GB/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => {
      expect(modelApiMocks.apiFetchVoid).toHaveBeenCalledWith(
        '/api/system/models/qwen3-embedding%3A0.6b',
        { method: 'DELETE' },
      );
    });
  });

  it('never offers deletion for the active local assignment', async () => {
    renderComponent({ value: localSmart.id });
    await screen.findByTestId('change-model-smart');
    expect(screen.queryByRole('button', { name: 'Install & manage local models' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete model Qwen3 14B' })).not.toBeInTheDocument();
  });

  it('does not expose local lifecycle controls for a cloud route', async () => {
    renderComponent({ value: 'openai/gpt-4o' });
    await screen.findByTestId('change-model-smart');
    expect(screen.queryByRole('button', { name: 'Install & manage local models' })).not.toBeInTheDocument();
  });

  it('keeps an unfit local model visible but disabled in the picker', async () => {
    renderComponent();
    await openPicker();
    const row = screen.getByTestId('model-row-qwen3:30b-a3b');
    expect(row).toHaveTextContent('Requires more VRAM.');
    expect(within(row).getByRole('button', { name: 'Use Qwen3 30B-A3B' })).toBeDisabled();
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { ModelSelector } from '@/components/shared/ModelSelector';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// Mock Radix Select with native HTML elements (portals don't work in jsdom)
// Store the onValueChange callback so SelectItem can call it
let selectOnValueChange: ((value: string) => void) | undefined;

vi.mock('@/components/ui/select', () => ({
  Select: ({ children, value, onValueChange }: any) => {
    selectOnValueChange = onValueChange;
    return (
      <div data-testid="select-root" data-value={value}>
        {typeof children === 'function' ? children({ value }) : children}
      </div>
    );
  },
  SelectTrigger: ({ children }: any) => (
    <button data-testid="select-trigger">{children}</button>
  ),
  SelectValue: ({ placeholder }: any) => (
    <span data-testid="select-value">{placeholder}</span>
  ),
  SelectContent: ({ children }: any) => (
    <div data-testid="select-content">{children}</div>
  ),
  SelectGroup: ({ children }: any) => <div data-testid="select-group">{children}</div>,
  SelectLabel: ({ children }: any) => <div data-testid="select-label">{children}</div>,
  SelectSeparator: () => <hr data-testid="select-separator" />,
  SelectItem: ({ children, value, disabled }: any) => (
    <div
      data-testid={`select-item-${value}`}
      aria-disabled={disabled ? 'true' : undefined}
      onClick={() => {
        if (!disabled) selectOnValueChange?.(value);
      }}
      role="option"
    >
      {children}
    </div>
  ),
}));

const modelApiMocks = vi.hoisted(() => ({
  fetchSystemModels: vi.fn(),
  apiFetchVoid: vi.fn(),
}));

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  const mocked = await createApiMock({
    fetchSystemModels: modelApiMocks.fetchSystemModels,
    apiFetchVoid: modelApiMocks.apiFetchVoid,
  });
  return mocked;
});

function renderComponent(props: Partial<React.ComponentProps<typeof ModelSelector>> = {}) {
  const queryClient = createTestQueryClient();
  const defaultProps = {
    value: '',
    onChange: vi.fn(),
    ...props,
  };
  return renderWithProviders(
    <ModelSelector {...defaultProps} />,
    { queryClient },
  );
}

const defaultModels = {
  status: 'ok',
  installed: [],
  hardware: { ollama_running: 1 },
  current: { smart_model: 'qwen3:14b' },
  issues: {},
  catalog: [
    {
      id: 'qwen3:14b',
      name: 'Qwen3 14B',
      provider: 'ollama',
      ollama_tag: 'qwen3:14b',
      roles: ['smart'],
      vram_gb: 9.5,
      disk_gb: 9.2,
      context_tokens: 32768,
      license: 'Apache 2.0',
      tier: 2,
      description: 'Strong reasoning for scientific text.',
      notes: '',
      last_reviewed: '2026-05-03',
      status: 'active',
      active: true,
      pulled: true,
      provider_key_present: false,
      fit: 'fits',
      can_assign: true,
      assign_blocker: null,
      size: 4.1e9,
      quantization: 'Q4_0',
    },
    {
      id: 'qwen3:4b',
      name: 'Qwen3 4B',
      provider: 'ollama',
      ollama_tag: 'qwen3:4b',
      roles: ['fast'],
      vram_gb: 3.5,
      disk_gb: 2.5,
      context_tokens: 32768,
      license: 'Apache 2.0',
      tier: 1,
      description: 'Fast local model.',
      notes: '',
      last_reviewed: '2026-05-03',
      status: 'downloadable',
      active: false,
      pulled: false,
      provider_key_present: false,
      fit: 'fits',
      can_assign: false,
      assign_blocker: 'Pull this model before assigning it.',
      quantization: 'Q8_0',
    },
    {
      id: 'qwen3-embedding:0.6b',
      name: 'Qwen3 Embedding 0.6B',
      provider: 'ollama',
      ollama_tag: 'qwen3-embedding:0.6b',
      roles: ['embed'],
      vram_gb: 1.2,
      disk_gb: 0.6,
      context_tokens: 8192,
      license: 'Apache 2.0',
      tier: 0,
      description: 'Embedding model.',
      notes: '',
      last_reviewed: '2026-05-03',
      status: 'pulled',
      active: false,
      pulled: true,
      provider_key_present: false,
      fit: 'fits',
      can_assign: true,
      assign_blocker: null,
    },
    {
      id: 'qwen3:30b-a3b',
      name: 'Qwen3 30B-A3B',
      provider: 'ollama',
      ollama_tag: 'qwen3:30b-a3b',
      roles: ['smart'],
      vram_gb: 19,
      disk_gb: 17,
      context_tokens: 32768,
      license: 'Apache 2.0',
      tier: 3,
      description: 'Large local model.',
      notes: '',
      last_reviewed: '2026-05-03',
      status: 'unfit',
      active: false,
      pulled: false,
      provider_key_present: false,
      fit: 'requires more VRAM',
      can_assign: false,
      assign_blocker: 'Requires more VRAM.',
    },
    {
      id: 'anthropic/claude-haiku-4-5',
      name: 'Claude Haiku 4.5',
      provider: 'anthropic',
      ollama_tag: null,
      roles: ['smart', 'fast'],
      vram_gb: 0,
      disk_gb: 0,
      context_tokens: 200000,
      license: 'Commercial',
      tier: 0,
      description: 'Fast cloud model.',
      notes: '',
      last_reviewed: '2026-05-03',
      status: 'cloud_required',
      active: false,
      pulled: false,
      provider_key_present: false,
      fit: 'cloud',
      can_assign: false,
      assign_blocker: 'Add an Anthropic API key before assigning this model.',
    },
    {
      id: 'openai/gpt-4o',
      name: 'GPT-4o',
      provider: 'openai',
      ollama_tag: null,
      roles: ['smart'],
      vram_gb: 0,
      disk_gb: 0,
      context_tokens: 128000,
      license: 'Commercial',
      tier: 0,
      description: 'Cloud reasoning model.',
      notes: '',
      last_reviewed: '2026-05-03',
      status: 'cloud_required',
      active: false,
      pulled: false,
      provider_key_present: true,
      fit: 'cloud',
      can_assign: true,
      assign_blocker: null,
    },
  ],
  recommendations: {},
};

describe('ModelSelector', () => {
  beforeEach(async () => {
    vi.clearAllMocks();

    modelApiMocks.fetchSystemModels.mockResolvedValue(defaultModels);
  });

  it('renders trigger with "Select a model" placeholder', () => {
    renderComponent();
    expect(screen.getByText('Select a model')).toBeInTheDocument();
  });

  it('filters role-compatible models from catalog entries', async () => {
    renderComponent({ configKey: 'llm.fast_model' });
    await waitFor(() => {
      expect(screen.getByText('Qwen3 4B')).toBeInTheDocument();
    });
    expect(screen.getByText('Q8_0')).toBeInTheDocument();
    expect(screen.getByText('(2.5GB disk)')).toBeInTheDocument();
    expect(screen.getByText('3.5GB VRAM')).toBeInTheDocument();
    expect(screen.queryByText('Qwen3 14B')).not.toBeInTheDocument();
    expect(screen.queryByText('Qwen3 Embedding 0.6B')).not.toBeInTheDocument();
    expect(screen.getByText('Ollama (default)')).toBeInTheDocument();
  });

  it('shows current badge when catalog entry is active for the role', async () => {
    renderComponent({ value: 'qwen3:14b', configKey: 'llm.smart_model' });
    await waitFor(() => {
      expect(screen.getByText('current')).toBeInTheDocument();
    });
    const currentBadges = screen.getAllByText('current');
    expect(currentBadges).toHaveLength(1);
  });

  it('shows downloadable local catalog entries but does not allow assigning them', async () => {
    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.fast_model' });
    await waitFor(() => {
      expect(screen.getByText('Qwen3 4B')).toBeInTheDocument();
      expect(screen.getByText('downloadable')).toBeInTheDocument();
    });
    expect(screen.getByText('Pull this model before assigning it.')).toBeInTheDocument();

    const option = screen.getByTestId('select-item-qwen3:4b');
    expect(option).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(option);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('shows unfit local catalog entries but does not allow assigning them', async () => {
    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.smart_model' });
    await waitFor(() => {
      expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
    });
    expect(screen.getByText('Qwen3 30B-A3B')).toBeInTheDocument();
    expect(screen.getByText('Requires more VRAM.')).toBeInTheDocument();
    const option = screen.getByTestId('select-item-qwen3:30b-a3b');
    expect(option).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(option);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('shows cloud entries and disables missing-key providers with a clear reason', async () => {
    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.smart_model' });
    await waitFor(() => {
      expect(screen.getByText('OpenAI')).toBeInTheDocument();
      expect(screen.getByText('GPT-4o')).toBeInTheDocument();
    });

    expect(screen.getByText('Anthropic')).toBeInTheDocument();
    expect(screen.getByText('Claude Haiku 4.5')).toBeInTheDocument();
    expect(screen.getByText('Add an Anthropic API key before assigning this model.')).toBeInTheDocument();

    const disabledCloud = screen.getByTestId('select-item-anthropic/claude-haiku-4-5');
    expect(disabledCloud).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(disabledCloud);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('renders detected hardware and per-model hardware requirements', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      hardware: {
        vram_gb: 16,
        vram_source: 'nvidia-smi',
        tier: 2,
        detected_at: '2026-05-06T10:00:00Z',
      },
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Detected hardware')).toBeInTheDocument();
    });
    expect(screen.getByText('16GB VRAM')).toBeInTheDocument();
    expect(screen.getByText('Tier 2')).toBeInTheDocument();
    expect(screen.getByText('9.5GB VRAM')).toBeInTheDocument();
  });

  it('honors backend-owned assignment blockers over derived local status', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: {},
      catalog: [
        {
          ...defaultModels.catalog[0],
          active: true,
          pulled: true,
          status: 'active',
          can_assign: false,
          assign_blocker: 'Backend assignment policy blocked this model.',
        },
      ],
    });
    const onChange = vi.fn();

    renderComponent({ onChange, configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Backend assignment policy blocked this model.')).toBeInTheDocument();
    });
    const option = screen.getByTestId('select-item-qwen3:14b');
    expect(option).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(option);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('shows "No models found. Is Ollama running?" when catalog is empty', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      status: 'ok',
      installed: [],
      hardware: {},
      current: {},
      issues: {},
      catalog: [],
      recommendations: {},
    });

    renderComponent();
    await waitFor(() => {
      expect(screen.getByText('No models found. Is Ollama running?')).toBeInTheDocument();
    });
  });

  it('shows degraded backend issue text instead of an empty-state guess', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      status: 'degraded',
      installed: [],
      hardware: {},
      current: {},
      issues: { installed: 'Could not load installed Ollama models.' },
      catalog: [],
      recommendations: {},
    });

    renderComponent();
    await waitFor(() => {
      expect(screen.getByText('Could not load installed Ollama models.')).toBeInTheDocument();
      expect(screen.getByText('No models available.')).toBeInTheDocument();
    });
  });

  it('shows query failures as errors instead of an empty-state message', async () => {

    modelApiMocks.fetchSystemModels.mockRejectedValue(new Error('boom'));

    renderComponent();
    await waitFor(() => {
      expect(
        screen.getByText('Could not load models. Check the API and Ollama status.'),
      ).toBeInTheDocument();
    });
  });

  it('calls onChange with catalog id when assignable local item is selected', async () => {
    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.smart_model' });
    await waitFor(() => {
      expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('select-item-qwen3:14b'));
    expect(onChange).toHaveBeenCalledWith('qwen3:14b');
  });

  it('calls onChange with cloud catalog id when keyed cloud item is selected', async () => {
    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.smart_model' });
    await waitFor(() => {
      expect(screen.getByText('GPT-4o')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('select-item-openai/gpt-4o'));
    expect(onChange).toHaveBeenCalledWith('openai/gpt-4o');
  });

  it('offers a pull action for the selected downloadable local model', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue(defaultModels);

    renderComponent({ value: 'qwen3:4b', configKey: 'llm.fast_model' });

    await waitFor(() => {
      expect(screen.getByText('Install & manage models')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Install & manage models'));
    fireEvent.click(screen.getByRole('button', { name: 'Pull model Qwen3 4B' }));

    await waitFor(() => {
      expect(screen.getByText('Pull Model')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));

    await waitFor(() => {
      expect(modelApiMocks.apiFetchVoid).toHaveBeenCalledWith('/api/system/models/qwen3%3A4b/pull', {
        method: 'POST',
      });
    });
  });

  it('offers a pull action for downloadable local models while the current value remains selected', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      catalog: [
        defaultModels.catalog[0],
        {
          ...defaultModels.catalog[1],
          id: 'qwen3:8b',
          name: 'Qwen3 8B',
          ollama_tag: 'qwen3:8b',
          roles: ['smart'],
          disk_gb: 4.9,
          vram_gb: 5.5,
          tier: 1,
          quantization: 'Q4_K_M',
        },
      ],
    });

    renderComponent({ value: 'qwen3:14b', configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByTestId('select-root')).toHaveAttribute('data-value', 'qwen3:14b');
      expect(screen.getByText('Install & manage models')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Install & manage models'));
    fireEvent.click(screen.getByRole('button', { name: 'Pull model Qwen3 8B' }));

    await waitFor(() => {
      expect(screen.getByText('Pull Model')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));

    await waitFor(() => {
      expect(modelApiMocks.apiFetchVoid).toHaveBeenCalledWith('/api/system/models/qwen3%3A8b/pull', {
        method: 'POST',
      });
    });
  });

  it('does not assign a downloadable local model before its pull succeeds', async () => {

    let resolvePull!: () => void;
    const pullPromise = new Promise<void>((resolve) => {
      resolvePull = resolve;
    });
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      catalog: [
        defaultModels.catalog[0],
        {
          ...defaultModels.catalog[1],
          id: 'qwen3:8b',
          name: 'Qwen3 8B',
          ollama_tag: 'qwen3:8b',
          roles: ['smart'],
        },
      ],
    });
    modelApiMocks.apiFetchVoid.mockImplementation(() => pullPromise);
    const onChange = vi.fn();

    renderComponent({ value: 'qwen3:14b', onChange, configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Install & manage models')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Install & manage models'));
    fireEvent.click(screen.getByRole('button', { name: 'Pull model Qwen3 8B' }));

    await waitFor(() => {
      expect(screen.getByText('Pull Model')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));

    await waitFor(() => {
      expect(modelApiMocks.apiFetchVoid).toHaveBeenCalledWith('/api/system/models/qwen3%3A8b/pull', {
        method: 'POST',
      });
    });
    expect(onChange).not.toHaveBeenCalled();
    resolvePull();
  });

  it('requires confirmation before deleting selected inactive pulled local models', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue(defaultModels);

    renderComponent({ value: 'qwen3-embedding:0.6b', configKey: 'llm.embed_model' });

    await waitFor(() => {
      expect(screen.getByText('Install & manage models')).toBeInTheDocument();
    });

    // Delete button is hidden until the manage section is expanded
    expect(
      screen.queryByRole('button', { name: 'Delete model Qwen3 Embedding 0.6B' }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Install & manage models'));

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Delete model Qwen3 Embedding 0.6B' }),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Delete model Qwen3 Embedding 0.6B' }));
    expect(screen.getByText('Delete Model')).toBeInTheDocument();
    expect(
      screen.getByText('This removes Qwen3 Embedding 0.6B from Ollama and frees approximately 0.6 GB. You can pull it again later.'),
    ).toBeInTheDocument();
    expect(
      modelApiMocks.apiFetchVoid.mock.calls.some(
        ([path, init]) =>
          path === '/api/system/models/qwen3-embedding%3A0.6b' && init?.method === 'DELETE',
      ),
    ).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(modelApiMocks.apiFetchVoid).toHaveBeenCalledWith('/api/system/models/qwen3-embedding%3A0.6b', {
        method: 'DELETE',
      });
    });
  });

  it('shows active cloud catalog entries even when provider key status is not present', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      status: 'ok',
      installed: [],
      hardware: {},
      current: { smart_model: 'anthropic/claude-haiku-4-5' },
      issues: {},
      catalog: [
        {
          ...defaultModels.catalog[4],
          status: 'cloud_active',
          active: true,
          provider_key_present: false,
          can_assign: true,
          assign_blocker: null,
        },
      ],
      recommendations: {},
    });

    renderComponent({ value: 'anthropic/claude-haiku-4-5', configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Anthropic')).toBeInTheDocument();
      expect(screen.getByText('Claude Haiku 4.5')).toBeInTheDocument();
    });
    expect(screen.getAllByText('current')).toHaveLength(1);
  });

  it('does not show delete button for the active model assignment', async () => {
    renderComponent({ value: 'qwen3:14b', configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
    });
    expect(
      screen.queryByRole('button', { name: /delete model qwen3 14b/i }),
    ).not.toBeInTheDocument();
  });

  it('hides delete actions until manage section is expanded', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue(defaultModels);

    renderComponent({ value: 'qwen3-embedding:0.6b', configKey: 'llm.embed_model' });

    await waitFor(() => {
      expect(screen.getByText('Install & manage models')).toBeInTheDocument();
    });

    expect(
      screen.queryByRole('button', { name: /delete model/i }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Install & manage models'));

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Delete model Qwen3 Embedding 0.6B' }),
      ).toBeInTheDocument();
    });
  });

  it('normalizes :latest suffix when matching selected local models', async () => {
    renderComponent({ value: 'qwen3:14b:latest', configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByTestId('select-root')).toHaveAttribute('data-value', 'qwen3:14b');
    });
  });

  // -------------------------------------------------------------------------
  // fit_detail-based disabled state
  // -------------------------------------------------------------------------

  it('disables options whose fit_detail.default is "unfit"', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      hardware: { vram_gb: 16, tier: 2, machine_id: 'host-test-gpu' },
      catalog: [
        {
          ...defaultModels.catalog[0],
          // qwen3:14b fits
          fit_detail: {
            default: 'fits',
            at_num_ctx: 8192,
            required_vram_gb: 12.0,
            default_num_ctx: 8192,
            max_num_ctx: 32768,
            kv_cache_bytes_per_token: 1024,
          },
          supports_thinking: true,
          can_assign: true,
          assign_blocker: null,
        },
        {
          ...defaultModels.catalog[3], // qwen3:30b-a3b
          fit_detail: {
            default: 'unfit',
            at_num_ctx: 8192,
            required_vram_gb: 22.0,
            default_num_ctx: 8192,
            max_num_ctx: 32768,
            kv_cache_bytes_per_token: 2048,
          },
          can_assign: false,
          assign_blocker: null,
        },
      ],
    });

    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
      expect(screen.getByText('Qwen3 30B-A3B')).toBeInTheDocument();
    });

    // qwen3:14b should be enabled
    const fitsOption = screen.getByTestId('select-item-qwen3:14b');
    expect(fitsOption).not.toHaveAttribute('aria-disabled', 'true');

    // qwen3:30b-a3b should be disabled via fit_detail.default=unfit
    const unfitOption = screen.getByTestId('select-item-qwen3:30b-a3b');
    expect(unfitOption).toHaveAttribute('aria-disabled', 'true');

    // Clicking the unfit option should not trigger onChange
    fireEvent.click(unfitOption);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('shows Cloud badge for entries with fit_detail.default === "cloud"', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [
        {
          ...defaultModels.catalog[5], // openai/gpt-4o
          fit_detail: {
            default: 'cloud',
            at_num_ctx: 8192,
            required_vram_gb: null,
            default_num_ctx: 8192,
            max_num_ctx: 128000,
            kv_cache_bytes_per_token: null,
          },
          can_assign: true,
          assign_blocker: null,
        },
      ],
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('GPT-4o')).toBeInTheDocument();
    });
    // Cloud badge should be visible
    expect(screen.getByText('Cloud')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Routing divergence line (T1.3)
  // -------------------------------------------------------------------------

  it('shows routing divergence line when LiteLLM serves a different model than saved', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      routing: { smart: 'qwen3:8b' },
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByTestId('routing-diverged-smart')).toBeInTheDocument();
    });
    const line = screen.getByTestId('routing-diverged-smart');
    expect(line).toHaveTextContent('You selected "qwen3:14b" but the system is currently using "qwen3:8b".');
  });

  it('does not show routing divergence line when routing matches saved model', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      routing: { smart: 'qwen3:14b' },
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('routing-diverged-smart')).not.toBeInTheDocument();
  });

  it('does not show routing divergence line when routing is absent (backend pre-T1.3)', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      // routing absent — older backend
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('routing-diverged-smart')).not.toBeInTheDocument();
  });

  // DA-07: effectiveFit predicate unification
  it('excludes a downloadable entry with fit_detail.default="unfit" from pull CTAs', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      hardware: { vram_gb: 4, tier: 1, vram_source: 'nvidia-smi' },
      current: { smart_model: 'qwen3:14b' },
      catalog: [
        defaultModels.catalog[0], // qwen3:14b — active, fits
        {
          // A model that reports fit:'fits' at the top level but whose
          // fit_detail.default is 'unfit' (e.g. VRAM check at configured num_ctx).
          id: 'qwen3:8b',
          name: 'Qwen3 8B',
          provider: 'ollama',
          ollama_tag: 'qwen3:8b',
          roles: ['smart'],
          vram_gb: 5.5,
          disk_gb: 4.9,
          context_tokens: 32768,
          license: 'Apache 2.0',
          tier: 1,
          description: 'Mid-size local model.',
          notes: '',
          last_reviewed: '2026-05-18',
          // top-level fit says 'fits' — this is the stale/coarse backend value
          status: 'downloadable',
          active: false,
          pulled: false,
          provider_key_present: false,
          fit: 'fits',
          can_assign: false,
          assign_blocker: 'Pull this model before assigning it.',
          quantization: 'Q4_K_M',
          // fit_detail.default says 'unfit' — VRAM-aware value; must win
          fit_detail: {
            default: 'unfit',
            at_num_ctx: 8192,
            required_vram_gb: 6.0,
            default_num_ctx: 8192,
            max_num_ctx: 32768,
            kv_cache_bytes_per_token: 1024,
          },
        },
      ],
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Qwen3 14B')).toBeInTheDocument();
      expect(screen.getByText('Qwen3 8B')).toBeInTheDocument();
    });

    // The model must NOT appear as a pull button CTA (neither recommended nor in pull list)
    expect(
      screen.queryByRole('button', { name: /pull model qwen3 8b/i }),
    ).not.toBeInTheDocument();
    // It must NOT be the recommended pull (setup-needed banner)
    expect(
      screen.queryByRole('button', { name: /pull qwen3 8b to get started/i }),
    ).not.toBeInTheDocument();

    // The row itself is still rendered but disabled (row-disable not changed)
    const option = screen.getByTestId('select-item-qwen3:8b');
    expect(option).toHaveAttribute('aria-disabled', 'true');
  });


  it('does not offer pull or delete controls for cloud catalog entries', async () => {

    const cloudEntries = defaultModels.catalog.filter((entry) =>
      ['anthropic', 'openai'].includes(entry.provider),
    );
    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      current: { smart_model: 'qwen3:14b' },
      catalog: [defaultModels.catalog[0], ...cloudEntries],
    });

    renderComponent({ value: 'qwen3:14b', configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('GPT-4o')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: /pull model gpt-4o/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete model gpt-4o/i })).not.toBeInTheDocument();
    expect(screen.queryByText('Install & manage models')).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Live provider-fetched entries (Task 9)
  // -------------------------------------------------------------------------

  it('renders a live provider-sourced entry as selectable inside its provider group', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [
        ...defaultModels.catalog,
        {
          ...defaultModels.catalog[4], // anthropic/claude-haiku-4-5 shape
          id: 'anthropic/claude-live-model',
          name: 'Claude Live Model',
          status: 'cloud_active',
          provider_key_present: true,
          can_assign: true,
          assign_blocker: null,
          source: 'provider',
          fetched_at: '2026-08-01T00:00:00Z',
        },
      ],
      provider_lists: {
        anthropic: { fetched_at: '2026-08-01T00:00:00Z', error: null, truncated: false },
      },
    });

    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Claude Live Model')).toBeInTheDocument();
    });

    const option = screen.getByTestId('select-item-anthropic/claude-live-model');
    expect(option).not.toHaveAttribute('aria-disabled', 'true');
    expect(within(option).queryByText(/API key/)).not.toBeInTheDocument();

    fireEvent.click(option);
    expect(onChange).toHaveBeenCalledWith('anthropic/claude-live-model');
  });

  it('renders a display-only unknown-capability entry as disabled with its blocker notes', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      catalog: [
        ...defaultModels.catalog,
        {
          ...defaultModels.catalog[4],
          id: 'anthropic/claude-mystery-model',
          name: 'Claude Mystery Model',
          status: 'cloud_required',
          provider_key_present: true,
          can_assign: false,
          assign_blocker: 'This provider did not say what this model can do, so JARVIS will not offer it for a role.',
          source: 'provider',
          fetched_at: '2026-08-01T00:00:00Z',
        },
      ],
      provider_lists: {
        anthropic: { fetched_at: '2026-08-01T00:00:00Z', error: null, truncated: false },
      },
    });

    const onChange = vi.fn();
    renderComponent({ onChange, configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('Claude Mystery Model')).toBeInTheDocument();
    });
    expect(
      screen.getByText('This provider did not say what this model can do, so JARVIS will not offer it for a role.'),
    ).toBeInTheDocument();

    const option = screen.getByTestId('select-item-anthropic/claude-mystery-model');
    expect(option).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(option);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('renders a provider-lists-only group label with the unavailable caption when the catalog has no entry for it', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      // No 'deepseek' entry anywhere in catalog — only provider_lists carries it.
      provider_lists: {
        deepseek: { fetched_at: null, error: 'provider request failed', truncated: false },
      },
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('DeepSeek')).toBeInTheDocument();
    });
    expect(
      screen.getByText('Model list unavailable — add a working key or restore connectivity'),
    ).toBeInTheDocument();
  });

  it('does not call a successful but empty group unavailable', async () => {

    modelApiMocks.fetchSystemModels.mockResolvedValue({
      ...defaultModels,
      provider_lists: {
        deepseek: { fetched_at: '2026-08-01T00:00:00Z', error: null, truncated: false },
      },
    });

    renderComponent({ configKey: 'llm.smart_model' });

    await waitFor(() => {
      expect(screen.getByText('DeepSeek')).toBeInTheDocument();
    });
    expect(
      screen.getByText('This provider offered no models JARVIS can use for this role'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Model list unavailable — add a working key or restore connectivity'),
    ).not.toBeInTheDocument();
  });
});

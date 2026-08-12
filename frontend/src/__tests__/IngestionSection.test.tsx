/**
 * IngestionSection — hardware-aware settings tests (Contract 06).
 *
 * Covers:
 * - Hardware strip shows VRAM + tier; does NOT show hostname
 * - Slider renders per LLM role
 * - num_ctx persists via setConfig on release
 * - Fit badge color flips green → yellow → red as slider crosses 85%/120% thresholds
 * - Thinking-mode checkbox renders for supports_thinking entries and persists
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { IngestionSection } from '@/components/settings/IngestionSection';
import { docsUrl } from '@/lib/docs-links';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const ingestionApiMocks = vi.hoisted(() => ({
  fetchSystemModels: vi.fn(),
  listProviders: vi.fn(),
}));

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  const mocked = await createApiMock({
    fetchConfig: vi.fn(),
    setConfig: async () => ({}),
    fetchSystemModels: ingestionApiMocks.fetchSystemModels,
    listProviders: ingestionApiMocks.listProviders,
  });
  return mocked;
});

// Mock Radix Slider with a simple range input for testability
vi.mock('@/components/ui/slider', () => ({
  Slider: ({ min, max, step, value, onValueChange, onValueCommit, 'data-testid': testid, 'aria-label': ariaLabel }: {
    min: number;
    max: number;
    step: number;
    value: number[];
    onValueChange?: (v: number[]) => void;
    onValueCommit?: (v: number[]) => void;
    'data-testid'?: string;
    'aria-label'?: string;
  }) => (
    <input
      type="range"
      data-testid={testid ?? 'slider'}
      aria-label={ariaLabel}
      min={min}
      max={max}
      step={step}
      value={value[0]}
      onChange={(e) => onValueChange?.([Number(e.target.value)])}
      onMouseUp={(e) => onValueCommit?.([Number((e.target as HTMLInputElement).value)])}
    />
  ),
}));

// Mock ModelSelector (not under test here). Exposes a change button so tests
// can drive the model-save mutation per config key.
vi.mock('@/components/shared/ModelSelector', () => ({
  ModelSelector: ({ value, onChange, configKey }: {
    value: string;
    onChange?: (v: string) => void;
    configKey?: string;
  }) => (
    <div data-testid="model-selector">
      {value}
      <button
        type="button"
        data-testid={`model-change-${configKey}`}
        onClick={() => onChange?.('changed-model:1b')}
      >
        change
      </button>
    </div>
  ),
}));

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

const baseConfig = [
  { key: 'llm.smart_model', value: 'qwen3:14b' },
  { key: 'llm.fast_model', value: 'qwen3:4b' },
  { key: 'llm.embed_model', value: 'qwen3-embedding:0.6b' },
];

const hardwareWith16GB = {
  vram_gb: 15.9,
  vram_source: 'nvidia-smi',
  tier: 2,
  detected_at: '2026-05-07T10:00:00Z',
  machine_id: 'host-test-gpu',
};

/** fit_detail for qwen3:14b at 8192 default context on a 16 GB box — fits */
const fitDetailFits = {
  default: 'fits' as const,
  at_num_ctx: 8192,
  required_vram_gb: 12.0,
  base_vram_gb: 12.0,
  base_num_ctx: 8192,
  default_num_ctx: 8192,
  max_num_ctx: 32768,
  kv_cache_bytes_per_token: 1000000, // 1 MB/token → large jumps for testing
};

const systemModelsWithFitDetail = {
  status: 'ok',
  installed: [],
  hardware: hardwareWith16GB,
  current: { smart_model: 'qwen3:14b' },
  issues: {},
  catalog: [
    {
      id: 'qwen3:14b',
      name: 'Qwen3 14B',
      provider: 'ollama',
      roles: ['smart'],
      fit_detail: fitDetailFits,
      supports_thinking: true,
    },
    {
      id: 'qwen3:4b',
      name: 'Qwen3 4B',
      provider: 'ollama',
      roles: ['fast'],
      fit_detail: {
        default: 'fits' as const,
        at_num_ctx: 8192,
        required_vram_gb: 3.0,
        base_vram_gb: 3.0,
        base_num_ctx: 8192,
        default_num_ctx: 8192,
        max_num_ctx: 32768,
        kv_cache_bytes_per_token: 500000,
      },
      supports_thinking: false,
    },
    {
      id: 'qwen3-embedding:0.6b',
      name: 'Qwen3 Embedding 0.6B',
      provider: 'ollama',
      roles: ['embed'],
      fit_detail: {
        default: 'fits' as const,
        at_num_ctx: 8192,
        required_vram_gb: 1.0,
        base_vram_gb: 1.0,
        base_num_ctx: 8192,
        default_num_ctx: 8192,
        max_num_ctx: 8192,
        kv_cache_bytes_per_token: 128000,
      },
      supports_thinking: false,
    },
  ],
};

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function renderSection() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <IngestionSection />,
    { queryClient },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('IngestionSection — hardware strip', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);
    ingestionApiMocks.listProviders.mockResolvedValue([]);
  });

  it('shows VRAM and tier in hardware strip', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    expect(screen.getByTestId('hardware-strip').textContent).toMatch(/15\.9 GB VRAM/);
    expect(screen.getByTestId('hardware-strip').textContent).toMatch(/Tier 2/);
  });

  it('does NOT show hostname in the hardware strip', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    // machine_id 'host-test-gpu' must never appear in the visible strip text
    expect(screen.getByTestId('hardware-strip').textContent).not.toMatch(/host-test-gpu/);
    // The whole rendered output should not surface the hostname either
    expect(document.body.textContent).not.toMatch(/host-test-gpu/);
  });

  it('expands to show vram_source and detected_at on click', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    // Before expand — source not visible
    expect(screen.queryByText(/nvidia-smi/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('hardware-strip'));
    // After expand
    expect(screen.getByText(/nvidia-smi/)).toBeInTheDocument();
  });

  it('pairs the hardware strip toggle with its content via aria-controls', async () => {
    renderSection();
    const toggle = await screen.findByTestId('hardware-strip');
    expect(toggle).toHaveAttribute('aria-controls', 'hardware-strip-details');

    fireEvent.click(toggle);
    const detail = await screen.findByText(/nvidia-smi/);
    expect(document.getElementById('hardware-strip-details')).toContainElement(detail);
  });

  it('renders hardware strip only in AI models group context', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    // Only one strip
    expect(screen.getAllByTestId('hardware-strip')).toHaveLength(1);
  });
});

describe('IngestionSection — unavailable cloud catalog', () => {
  it('keeps a configured cloud route truthful when its catalog entry is unavailable', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig.filter((entry) => entry.key !== 'llm.smart_model'),
      { key: 'llm.smart_model', value: 'openrouter/vendor/unlisted-model' },
    ]);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      current: {
        ...systemModelsWithFitDetail.current,
        smart_model: 'openrouter/vendor/unlisted-model',
      },
      catalog: systemModelsWithFitDetail.catalog,
      provider_lists: {
        openrouter: {
          model_count: 0,
          fetched_at: '2026-08-11T00:00:00Z',
          error: 'provider request failed',
          truncated: false,
          excluded: {},
        },
      },
    });
    ingestionApiMocks.listProviders.mockResolvedValue([
      {
        id: 'openrouter',
        display_name: 'OpenRouter',
        kind: 'router',
        api_key_config_key: 'llm.providers.openrouter.api_key',
        base_url_config_key: null,
        assignment_prefix: 'openrouter/',
        litellm_prefix: 'openrouter/',
        privacy_boundary: 'router',
        best_for: 'Hosted models',
        data_note: 'Requests pass through OpenRouter to the selected upstream provider.',
        configured: true,
        base_url_configured: false,
        supports_assignment: true,
        dashboard_url: 'https://openrouter.ai/settings/keys',
        account_capability: 'current_key',
      },
    ]);

    renderSection();

    const card = await screen.findByTestId('llm-route-card-smart');
    expect(card).toHaveTextContent('Cloud — through OpenRouter');
    expect(card).toHaveTextContent('Provider catalog is currently unavailable');
    expect(card).toHaveTextContent('Price unavailable');
    expect(card).not.toHaveTextContent('No provider charge');
    expect(screen.queryByTestId('configure-toggle-smart')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Provider details' })).toBeInTheDocument();
  });
});

describe('IngestionSection — num_ctx slider', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);
  });

  it('renders local controls for generative routes, but not the dimension-locked embedder', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
      expect(screen.getByTestId('configure-toggle-fast')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('configure-toggle-embed')).not.toBeInTheDocument();
  });

  it('slider is not visible before Configure is opened', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('slider-smart')).not.toBeInTheDocument();
  });

  it('slider appears after opening Configure for smart role', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });
  });

  it('pairs the local-model-controls toggle with its content via aria-controls', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    const toggle = screen.getByTestId('configure-toggle-smart');
    expect(toggle).toHaveAttribute('aria-controls', 'local-model-controls-smart');

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });
    expect(document.getElementById('local-model-controls-smart')).toContainElement(
      screen.getByTestId('slider-smart'),
    );
  });

  it('slider for the smart role is reachable by its label', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByLabelText('Reading window for the main model')).toBe(
        screen.getByTestId('slider-smart'),
      );
    });
  });

  it('persists num_ctx via setConfig on slider commit (mouseup)', async () => {
    const { setConfig } = await import('@/lib/api');
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });

    const slider = screen.getByTestId('slider-smart');
    // Move slider to index 2 (8192 = 3rd stop)
    fireEvent.change(slider, { target: { value: '2' } });
    fireEvent.mouseUp(slider);

    await waitFor(() => {
      expect(setConfig).toHaveBeenCalledWith(
        'llm.host-test-gpu.smart_num_ctx',
        8192,
      );
    });
  });

  it('re-reads num_ctx from config on mount', async () => {
    const { fetchConfig } = await import('@/lib/api');
    // Provide a persisted value of 4096 (index 1)
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig,
      { key: 'llm.host-test-gpu.smart_num_ctx', value: 4096 },
    ]);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });

    // The slider should start at index 1 (4096)
    const slider = screen.getByTestId('slider-smart') as HTMLInputElement;
    expect(slider.value).toBe('1');
  });
});

describe('IngestionSection — num_ctx save failure honesty', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);
  });

  it('shows an inline error and rolls the slider back when num_ctx save fails', async () => {
    const { setConfig } = await import('@/lib/api');
    vi.mocked(setConfig).mockRejectedValueOnce(new Error('HTTP 400: rejected'));

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });

    const slider = screen.getByTestId('slider-smart') as HTMLInputElement;
    // Default 8192 = index 2; move down to index 1 (4096) and commit
    fireEvent.change(slider, { target: { value: '1' } });
    fireEvent.mouseUp(slider);

    await waitFor(() => {
      expect(setConfig).toHaveBeenCalledWith('llm.host-test-gpu.smart_num_ctx', 4096);
    });

    // Inline error rendered + slider rolled back to the persisted stop (8192)
    await waitFor(() => {
      expect(screen.getByTestId('num-ctx-save-error-smart')).toHaveTextContent(
        /Failed to save: HTTP 400: rejected/,
      );
    });
    expect(slider.value).toBe('2');
  });
});

describe('IngestionSection — per-key save errors', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);
  });

  it('paints the failed save error ONLY under the card whose key failed', async () => {
    const { setConfig } = await import('@/lib/api');
    vi.mocked(setConfig).mockRejectedValueOnce(new Error('HTTP 400: rejected'));

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('model-change-llm.smart_model')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('model-change-llm.smart_model'));

    await waitFor(() => {
      expect(screen.getByTestId('config-save-error-llm.smart_model')).toHaveTextContent(
        /Failed to save: HTTP 400: rejected/,
      );
    });
    // The identical error must NOT paint under the fast and embed cards.
    expect(screen.queryByTestId('config-save-error-llm.fast_model')).not.toBeInTheDocument();
    expect(screen.queryByTestId('config-save-error-llm.embed_model')).not.toBeInTheDocument();
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });
});

describe('IngestionSection — model delivery pending pill', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
  });

  it('renders the pending pill only for roles with delivery=pending_restart', async () => {

    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      delivery: { smart: 'pending_restart', fast: 'applied', embed: 'applied' },
    });

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('delivery-pending-smart')).toBeInTheDocument();
    });
    expect(screen.getByTestId('delivery-pending-smart').textContent).toMatch(
      /pending — applying automatically/,
    );
    expect(screen.queryByTestId('delivery-pending-fast')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delivery-pending-embed')).not.toBeInTheDocument();
  });

  it('renders no pill when the delivery field is absent (older backend)', async () => {

    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('delivery-pending-smart')).not.toBeInTheDocument();
  });
});

describe('IngestionSection — fit badge color', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
  });

  it('shows green fit badge at default (8192) ctx', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      // At default 8192, required=12.0 GB / 15.9 GB → 75.5% → fits (< 85%)
      expect(screen.getByTestId('fit-badge-fits')).toBeInTheDocument();
    });
  });

  it('shows yellow badge when required VRAM is between 85% and 120%', async () => {
    /**
     * To produce "partial": required > 0.85*15.9=13.515 AND required <= 0.120*15.9=19.08
     * With kv_cache_bytes_per_token=1_000_000 (1 MB/token) and default=8192:
     * slider at stop index 3 = 16384 → extra = 16384-8192 = 8192 tokens
     * required = 12.0 + 8192 * 1_000_000 / 1e9 = 12.0 + 8.192 = 20.192 → unfit
     *
     * So we need a model with lower base VRAM to hit the partial zone.
     * Use base_vram_gb=10.0, base=8192, kv=500_000:
     * At 16384: required = 10.0 + (16384-8192)*500_000/1e9 = 10.0 + 4.096 = 14.096 > 13.515 → partial
     */
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      hardware: { ...hardwareWith16GB },
      catalog: [
        {
          ...systemModelsWithFitDetail.catalog[0],
          fit_detail: {
            default: 'fits' as const,
            at_num_ctx: 8192,
            required_vram_gb: 10.0,
            base_vram_gb: 10.0,
            base_num_ctx: 8192,
            default_num_ctx: 8192,
            max_num_ctx: 32768,
            kv_cache_bytes_per_token: 500_000,
          },
        },
        ...systemModelsWithFitDetail.catalog.slice(1),
      ],
    });

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });

    // Move to index 3 = 16384 tokens
    const slider = screen.getByTestId('slider-smart');
    fireEvent.change(slider, { target: { value: '3' } });

    await waitFor(() => {
      // partial: 14.096 / 15.9 = 88.6% — above 85%, below 120%
      expect(screen.getByTestId('fit-badge-partial')).toBeInTheDocument();
    });
  });

  it('uses backend baseline fields instead of double-counting selected required VRAM', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig,
      { key: 'llm.host-test-gpu.smart_num_ctx', value: 16384 },
    ]);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      catalog: [
        {
          ...systemModelsWithFitDetail.catalog[0],
          fit_detail: {
            default: 'partial' as const,
            at_num_ctx: 16384,
            required_vram_gb: 14.915,
            base_vram_gb: 10.0,
            base_num_ctx: 8192,
            default_num_ctx: 8192,
            max_num_ctx: 32768,
            kv_cache_bytes_per_token: 600_000,
          },
        },
        ...systemModelsWithFitDetail.catalog.slice(1),
      ],
    });

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));

    await waitFor(() => {
      // Plain label on the pill; the GB ratio lives on the subordinate detail line.
      expect(screen.getByTestId('fit-badge-partial')).toHaveTextContent('Runs, but slower');
    });
    expect(screen.getByTestId('fit-detail-smart')).toHaveTextContent('14.9 GB / 15.9 GB');
    expect(screen.getByTestId('fit-badge-partial')).not.toHaveTextContent('14.9 GB / 15.9 GB');
    expect(screen.queryByTestId('fit-badge-unfit')).not.toBeInTheDocument();
  });

  it('falls back to backend verdict when additive baseline fields are absent', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig,
      { key: 'llm.host-test-gpu.smart_num_ctx', value: 16384 },
    ]);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      catalog: [
        {
          ...systemModelsWithFitDetail.catalog[0],
          fit_detail: {
            default: 'partial' as const,
            at_num_ctx: 16384,
            required_vram_gb: 14.915,
            default_num_ctx: 8192,
            max_num_ctx: 32768,
            kv_cache_bytes_per_token: 600_000,
          },
        },
        ...systemModelsWithFitDetail.catalog.slice(1),
      ],
    });

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));

    await waitFor(() => {
      expect(screen.getByTestId('fit-badge-partial')).toHaveTextContent('Runs, but slower');
    });
    // No additive baseline → no computed required VRAM → no GB/GB detail line.
    expect(screen.queryByText(/14\.9 GB \/ 15\.9 GB/)).not.toBeInTheDocument();
    expect(screen.queryByTestId('fit-detail-smart')).not.toBeInTheDocument();
  });

  it('shows red badge and clamps slider when required VRAM exceeds 120%', async () => {
    /**
     * Produce "unfit": required > 0.120*15.9=19.08
     * Use the original fitDetailFits with kv=1_000_000:
     * At 16384: required = 12.0 + 8192 * 1_000_000 / 1e9 = 20.192 > 19.08 → unfit
     * At 8192: required = 12.0 → fits
     * Slider clamp should snap back to index 2 (8192)
     */
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail); // kv=1_000_000

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });

    const slider = screen.getByTestId('slider-smart');
    // Try moving to index 3 (16384) — should be clamped to index 2 (8192)
    fireEvent.change(slider, { target: { value: '3' } });

    await waitFor(() => {
      // Clamped back to fits zone — badge stays green
      expect(screen.getByTestId('fit-badge-fits')).toBeInTheDocument();
    });

    // Slider value should reflect clamp (stays at 2)
    expect((slider as HTMLInputElement).value).toBe('2');
  });
});

describe('IngestionSection — thinking-mode toggle', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
  });

  it('renders thinking-mode toggle for supports_thinking models when Configure open', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));

    await waitFor(() => {
      expect(screen.getByTestId('thinking-toggle-smart')).toBeInTheDocument();
    });
    expect(screen.getByText(/Disable thinking mode/)).toBeInTheDocument();
  });

  it('does NOT render thinking-mode toggle for non-thinking models', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-fast')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-fast'));

    await waitFor(() => {
      expect(screen.getByTestId('slider-fast')).toBeInTheDocument();
    });
    // fast role's model has supports_thinking=false
    expect(screen.queryByTestId('thinking-toggle-fast')).not.toBeInTheDocument();
  });

  it('persists thinking_disabled via setConfig on toggle change', async () => {
    const { fetchConfig, setConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));

    await waitFor(() => {
      expect(screen.getByTestId('thinking-toggle-smart')).toBeInTheDocument();
    });

    // The switch in thinking toggle
    const switchEl = screen.getByRole('switch', { name: /disable thinking mode/i });
    fireEvent.click(switchEl);

    await waitFor(() => {
      expect(setConfig).toHaveBeenCalledWith(
        'llm.host-test-gpu.thinking_disabled.qwen3:14b',
        expect.any(Boolean),
      );
    });
  });
});

// ---------------------------------------------------------------------------
// Hardware Recommendation Banner (per-VRAM advisory)
// ---------------------------------------------------------------------------

const hwRecommendationMid = {
  vram_mb: 16384,
  bucket: 'MID',
  summary: 'Mid-tier GPU (10–19 GB) detected. Default stack fits with headroom.',
  aliases: [
    { alias: 'smart', model: 'qwen3:8b', confirm_on_target: false, notes: '' },
    { alias: 'fast', model: 'qwen3:4b', confirm_on_target: false, notes: '' },
    { alias: 'embed', model: 'qwen3-embedding:4b', confirm_on_target: false, notes: 'Use 4b for better quality' },
  ],
};

const hwRecommendationMidHigh = {
  vram_mb: 30720,
  bucket: 'MID_HIGH',
  summary: 'Mid-high GPU (20–39 GB) detected. Upgrade smart to qwen3:14b for better quality with ample headroom.',
  aliases: [
    { alias: 'smart', model: 'qwen3:14b', confirm_on_target: false, notes: '20–39 GB GPU; qwen3:14b (~9 GB) + embedder fits A10/3090' },
    { alias: 'fast', model: 'qwen3:4b', confirm_on_target: false, notes: '' },
    { alias: 'embed', model: 'qwen3-embedding:4b', confirm_on_target: false, notes: '' },
  ],
};

const hwRecommendationHigh = {
  vram_mb: 49152,
  bucket: 'HIGH',
  summary: 'High-end GPU (≥40 GB) detected. Full 32B stack fits.',
  aliases: [
    { alias: 'smart', model: 'qwen3:32b', confirm_on_target: true, notes: 'Confirm on target hardware before switching' },
    { alias: 'fast', model: 'qwen3:8b', confirm_on_target: false, notes: '' },
    { alias: 'embed', model: 'qwen3-embedding:4b', confirm_on_target: false, notes: '' },
  ],
};

const hwRecommendationNullVram = {
  vram_mb: null,
  bucket: 'CPU_ONLY',
  summary: 'GPU probe failed or no GPU detected. Running on CPU.',
  aliases: [],
};

describe('IngestionSection — hardware recommendation banner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows summary + alias rows for a MID bucket recommendation', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      // No seeded model → the per-VRAM recommendation is the single advisory.
      current: {},
      hardware_recommendation: hwRecommendationMid,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hw-recommendation-banner')).toBeInTheDocument();
    });

    // Summary text shown
    expect(screen.getByText('Mid-tier GPU (10–19 GB) detected. Default stack fits with headroom.')).toBeInTheDocument();

    // Per-alias recommended model rows (use getAllByText for models that may also
    // appear in the ModelSelector mock which renders the configured value)
    const aliasList = screen.getByTestId('hw-recommendation-alias-list');
    expect(aliasList.textContent).toContain('qwen3:8b');
    expect(aliasList.textContent).toContain('qwen3:4b');
    expect(aliasList.textContent).toContain('qwen3-embedding:4b');

    // Alias labels visible
    expect(screen.getByText('smart')).toBeInTheDocument();
    expect(screen.getByText('fast')).toBeInTheDocument();
    expect(screen.getByText('embed')).toBeInTheDocument();
  });

  it('shows summary + alias rows for a MID_HIGH bucket recommendation', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      current: {},
      hardware_recommendation: hwRecommendationMidHigh,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hw-recommendation-banner')).toBeInTheDocument();
    });

    // Summary text shown
    expect(screen.getByText('Mid-high GPU (20–39 GB) detected. Upgrade smart to qwen3:14b for better quality with ample headroom.')).toBeInTheDocument();

    // Per-alias recommended model rows
    const aliasList = screen.getByTestId('hw-recommendation-alias-list');
    expect(aliasList.textContent).toContain('qwen3:14b');
    expect(aliasList.textContent).toContain('qwen3:4b');
    expect(aliasList.textContent).toContain('qwen3-embedding:4b');

    // Alias labels visible
    expect(screen.getByText('smart')).toBeInTheDocument();
    expect(screen.getByText('fast')).toBeInTheDocument();
    expect(screen.getByText('embed')).toBeInTheDocument();
  });

  it('marks advisory framing — does not claim auto-change', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      current: {},
      hardware_recommendation: hwRecommendationMid,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hw-recommendation-banner')).toBeInTheDocument();
    });

    // Banner must contain advisory framing text
    const banner = screen.getByTestId('hw-recommendation-banner');
    expect(banner.textContent).toMatch(/advisory|recommended|does not change/i);
  });

  it('shows confirm-on-target badge for aliases with confirm_on_target:true', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      current: {},
      hardware_recommendation: hwRecommendationHigh,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hw-recommendation-banner')).toBeInTheDocument();
    });

    // qwen3:32b row should have a confirm-on-target indicator
    expect(screen.getByTestId('confirm-on-target-smart')).toBeInTheDocument();

    // aliases without confirm_on_target should NOT show the indicator
    expect(screen.queryByTestId('confirm-on-target-fast')).not.toBeInTheDocument();
    expect(screen.queryByTestId('confirm-on-target-embed')).not.toBeInTheDocument();
  });

  it('renders gracefully when vram_mb is null and aliases is empty (GPU probe failed)', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      current: {},
      hardware_recommendation: hwRecommendationNullVram,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hw-recommendation-banner')).toBeInTheDocument();
    });

    // Summary is shown
    expect(screen.getByText('GPU probe failed or no GPU detected. Running on CPU.')).toBeInTheDocument();

    // No alias rows rendered (aliases: [])
    expect(screen.queryByTestId('hw-recommendation-alias-list')).not.toBeInTheDocument();
  });

  it('does NOT render banner when hardware_recommendation is absent', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });

    expect(screen.queryByTestId('hw-recommendation-banner')).not.toBeInTheDocument();
  });
});

describe('IngestionSection — degraded backend (no fit_detail)', () => {
  it('renders without crashing when fit_detail is absent', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      catalog: systemModelsWithFitDetail.catalog.map((c) => ({
        ...c,
        fit_detail: undefined,
      })),
    });

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('configure-toggle-smart'));
    await waitFor(() => {
      expect(screen.getByTestId('slider-smart')).toBeInTheDocument();
    });
    // No crash, no fit badge shown (unknown state)
    expect(screen.queryByTestId('fit-badge-fits')).not.toBeInTheDocument();
  });

  it('renders without crashing when hardware is absent', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      hardware: undefined,
    });

    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
    });
    // No hardware strip when hardware is absent
    expect(screen.queryByTestId('hardware-strip')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Part 3 — new FE hunks (T2.4)
// ---------------------------------------------------------------------------

describe('IngestionSection — first-boot model banner (Part 3-1)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the CURRENT smart model (not the bucket recommendation) for the detected GPU', async () => {
    // current.smart_model is qwen3:14b (what autoconfigure actually seeded);
    // the bucket recommendation's smart alias is qwen3:8b. The banner must
    // assert the model that was actually picked — qwen3:14b, not qwen3:8b.
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      hardware_recommendation: hwRecommendationMid,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('first-boot-model-banner')).toBeInTheDocument();
    });

    const banner = screen.getByTestId('first-boot-model-banner');
    expect(banner.textContent).toMatch(/We picked/);
    // CURRENT smart model renders; the recommendation's qwen3:8b is NOT asserted.
    expect(banner.textContent).toMatch(/qwen3:14b/);
    expect(banner.textContent).not.toMatch(/qwen3:8b/);
    expect(banner.textContent).toMatch(/15\.9.*GB.*GPU/);
    expect(banner.textContent).toMatch(/change anytime in Settings.*Models/i);
  });

  it('does NOT render first-boot banner when there is no current smart model (even with a recommendation)', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      current: {},
      hardware_recommendation: hwRecommendationMid,
    });

    renderSection();

    // The advisory recommendation banner still renders (recommendation present)…
    await waitFor(() => {
      expect(screen.getByTestId('hw-recommendation-banner')).toBeInTheDocument();
    });
    // …but the first-boot banner is hidden because no current smart model exists.
    expect(screen.queryByTestId('first-boot-model-banner')).not.toBeInTheDocument();
  });

  it('does NOT render first-boot banner on CPU (no GPU / vram 0)', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      hardware: { ...hardwareWith16GB, vram_gb: 0 },
      hardware_recommendation: hwRecommendationNullVram,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hw-recommendation-banner')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('first-boot-model-banner')).not.toBeInTheDocument();
  });
});

describe('IngestionSection — hardware source line (Part 3-2)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders hardware-source-line when vram_source_detail is present', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      hardware: {
        ...hardwareWith16GB,
        vram_source_detail: 'nvidia-smi (GPU 0: RTX 4090, 24576 MiB)',
      },
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hardware-source-line')).toBeInTheDocument();
    });
    expect(screen.getByTestId('hardware-source-line').textContent).toContain('nvidia-smi (GPU 0: RTX 4090');
  });

  it('does NOT render hardware-source-line when vram_source_detail is absent', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('hardware-source-line')).not.toBeInTheDocument();
  });
});

describe('IngestionSection — GPU overlay divergence line (Part 3-3)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders gpu-overlay-divergence warning when host_gpu_divergence is true', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      hardware: {
        ...hardwareWith16GB,
        host_gpu_divergence: true,
      },
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('gpu-overlay-divergence')).toBeInTheDocument();
    });
    const line = screen.getByTestId('gpu-overlay-divergence');
    expect(line.textContent).toMatch(/15\.9.*GB.*detected on host/);
    expect(line.textContent).toMatch(/GPU overlay not active/);
  });

  it('does NOT render gpu-overlay-divergence when host_gpu_divergence is false', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue({
      ...systemModelsWithFitDetail,
      hardware: {
        ...hardwareWith16GB,
        host_gpu_divergence: false,
      },
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('gpu-overlay-divergence')).not.toBeInTheDocument();
  });

  it('does NOT render gpu-overlay-divergence when host_gpu_divergence is absent', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('gpu-overlay-divergence')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Researcher-language pass — model controls carry plain-language labels
// + a one-sentence "what this does" description for each role.
// ---------------------------------------------------------------------------

describe('IngestionSection — researcher-language model labels', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);
  });

  it('renders a plain-language top-of-page description for the AI models group', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('llm-models-description')).toBeInTheDocument();
    });
    expect(screen.getByTestId('llm-models-description').textContent).toMatch(/applies your choices automatically/i);
  });

  it('labels each route in plain language without exposing internal aliases', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('Main model')).toBeInTheDocument();
    });
    expect(screen.getByText('Quick model')).toBeInTheDocument();
    expect(screen.getByText('Embedding model')).toBeInTheDocument();
  });

  it('describes what each model does and that choices apply automatically', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/Writes summaries, cards, extraction, and Ask answers/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Scores and triages incoming papers/i)).toBeInTheDocument();
    expect(screen.getByText(/Builds the searchable representation of every paper/i)).toBeInTheDocument();
    // Post-W1/2/3 truth: model choices apply automatically (not "pending restart").
    expect(screen.getAllByText(/applies automatically/i).length).toBeGreaterThanOrEqual(2);
  });

  it('resolves local route details when a stored model value includes the latest suffix', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig.filter((entry) => entry.key !== 'llm.smart_model'),
      { key: 'llm.smart_model', value: 'qwen3:14b:latest' },
    ]);

    renderSection();

    const card = await screen.findByTestId('llm-route-card-smart');
    expect(card).toHaveTextContent('Fits this machine');
    expect(card).not.toHaveTextContent('Model details unavailable');
  });
});

// ---------------------------------------------------------------------------
// Config load failure — must render an error state, never the empty state.
// No existing test forces the config query to error; the empty-state branch
// previously absorbed failures (an invariant break).
// ---------------------------------------------------------------------------

describe('IngestionSection — config load failure', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders an error state (not EmptyState) when the config query fails', async () => {
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockRejectedValue(new Error('HTTP 500: boom'));
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('config-load-error')).toBeInTheDocument();
    });
    expect(screen.getByTestId('config-load-error')).toHaveTextContent(/Failed to load configuration/i);
    // The empty-state copy must NOT appear on failure.
    expect(screen.queryByText('No config entries')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Documentation links — routed through docsUrl() (src/lib/docs-links.ts) so
// the published-site base URL and path-to-URL shape live in one place.
// ---------------------------------------------------------------------------

describe('IngestionSection — embedding guide link', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    ingestionApiMocks.fetchSystemModels.mockResolvedValue(systemModelsWithFitDetail);
  });

  it('resolves the migration guide link through docsUrl, with no raw hostname in the component', async () => {
    renderSection();

    const link = await screen.findByRole('link', { name: /embedding model migration guide/i });
    expect(link).toHaveAttribute('href', docsUrl('manual/changing-embedding-model.md'));

    const componentSource = readFileSync(
      path.resolve(__dirname, '../components/settings/IngestionSection.tsx'),
      'utf8',
    );
    expect(componentSource).not.toContain('limitcycle-oss.github.io');
  });
});

describe('docsUrl() call sites name a real file under docs/', () => {
  const srcRoot = path.resolve(__dirname, '..');
  const docsRoot = path.resolve(__dirname, '../../../docs');
  const DOCS_HOSTNAME = 'limitcycle-oss.github.io';
  // Matches a docsUrl call whose sole argument is a single- or double-quoted
  // string literal, capturing the quoted path in group 2.
  const CALL_PATTERN = /\bdocsUrl\(\s*(['"])((?:(?!\1).)+)\1\s*\)/g;

  // Scan product source only. Test files are excluded so the scan can never
  // match its own regex definition or assertion strings — see this file's
  // "embedding guide link" test above for the one legitimate test-side call.
  function productFilesUnder(directory: string): string[] {
    return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      if (entry.name === '__tests__') return [];
      const absolute = path.join(directory, entry.name);
      return entry.isDirectory() ? productFilesUnder(absolute) : [absolute];
    }).filter((file) => (file.endsWith('.ts') || file.endsWith('.tsx')) && !file.endsWith('.test.tsx') && !file.endsWith('.test.ts'));
  }

  function docPathsUsed(): string[] {
    const paths: string[] = [];
    for (const file of productFilesUnder(srcRoot)) {
      for (const match of readFileSync(file, 'utf8').matchAll(CALL_PATTERN)) {
        const docPath = match[2];
        if (docPath !== undefined) paths.push(docPath);
      }
    }
    return paths;
  }

  it('every docsUrl(path) argument in the product source resolves to a file under docs/', () => {
    const docPaths = docPathsUsed();
    // A pattern miss would make this check vacuously pass — pin the known
    // call count so a regex regression fails loudly instead of silently.
    expect(docPaths.length).toBeGreaterThanOrEqual(3);

    for (const docPath of docPaths) {
      const exists = existsSync(path.resolve(docsRoot, docPath));
      expect(exists, `docsUrl call argument "${docPath}" has no matching file under docs/`).toBe(true);
    }
  });

  it('no product file other than docs-links.ts hardcodes the published docs hostname', () => {
    // A raw href is invisible to the docsUrl() scan above — guard separately
    // so a future component can't bypass docsUrl() with a literal URL.
    const offenders = productFilesUnder(srcRoot)
      .filter((file) => path.relative(srcRoot, file) !== path.join('lib', 'docs-links.ts'))
      .filter((file) => readFileSync(file, 'utf8').includes(DOCS_HOSTNAME));

    expect(offenders, `raw docs hostname found outside docs-links.ts: ${offenders.join(', ')}`).toEqual([]);
  });
});

/**
 * IngestionSection — hardware-aware settings tests (Contract 06 / Wave-5 T3-C).
 *
 * Covers:
 * - Hardware strip shows VRAM + tier; does NOT show hostname
 * - Slider renders per LLM role
 * - num_ctx persists via setConfig on release
 * - Fit badge color flips green → yellow → red as slider crosses 85%/120% thresholds
 * - Thinking-mode checkbox renders for supports_thinking entries and persists
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { IngestionSection } from '@/components/settings/IngestionSection';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  const apiFetch = vi.fn();
  // fetchSystemModels is the named export used by IngestionSection's queryFn.
  // Wire it through to the same apiFetch mock so existing per-test
  // `vi.mocked(apiFetch).mockResolvedValue(...)` calls control both.
  const fetchSystemModels = vi.fn().mockImplementation((_signal?: AbortSignal) => apiFetch('/api/system/models'));
  return {
    ...orig,
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({}),
    apiFetch,
    fetchSystemModels,
  };
});

// Mock Radix Slider with a simple range input for testability
vi.mock('@/components/ui/slider', () => ({
  Slider: ({ min, max, step, value, onValueChange, onValueCommit, 'data-testid': testid }: {
    min: number;
    max: number;
    step: number;
    value: number[];
    onValueChange?: (v: number[]) => void;
    onValueCommit?: (v: number[]) => void;
    'data-testid'?: string;
  }) => (
    <input
      type="range"
      data-testid={testid ?? 'slider'}
      min={min}
      max={max}
      step={step}
      value={value[0]}
      onChange={(e) => onValueChange?.([Number(e.target.value)])}
      onMouseUp={(e) => onValueCommit?.([Number((e.target as HTMLInputElement).value)])}
    />
  ),
}));

// Mock ModelSelector (not under test here)
vi.mock('@/components/shared/ModelSelector', () => ({
  ModelSelector: ({ value }: { value: string }) => (
    <div data-testid="model-selector">{value}</div>
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
  machine_id: 'host-rtx5060',
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <IngestionSection />
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('IngestionSection — hardware strip', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);
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
    // machine_id 'host-rtx5060' must never appear in the visible strip text
    expect(screen.getByTestId('hardware-strip').textContent).not.toMatch(/host-rtx5060/);
    // The whole rendered output should not surface the hostname either
    expect(document.body.textContent).not.toMatch(/host-rtx5060/);
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

  it('renders hardware strip only in LLM Models group context', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });
    // Only one strip
    expect(screen.getAllByTestId('hardware-strip')).toHaveLength(1);
  });
});

describe('IngestionSection — num_ctx slider', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);
  });

  it('renders a Configure toggle for each LLM role', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('configure-toggle-smart')).toBeInTheDocument();
      expect(screen.getByTestId('configure-toggle-fast')).toBeInTheDocument();
      expect(screen.getByTestId('configure-toggle-embed')).toBeInTheDocument();
    });
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
        'llm.host-rtx5060.smart_num_ctx',
        8192,
      );
    });
  });

  it('re-reads num_ctx from config on mount', async () => {
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    // Provide a persisted value of 4096 (index 1)
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig,
      { key: 'llm.host-rtx5060.smart_num_ctx', value: 4096 },
    ]);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);

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

describe('IngestionSection — fit badge color', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
  });

  it('shows green fit badge at default (8192) ctx', async () => {
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);

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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig,
      { key: 'llm.host-rtx5060.smart_num_ctx', value: 16384 },
    ]);
    vi.mocked(apiFetch).mockResolvedValue({
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
      expect(screen.getByTestId('fit-badge-partial')).toHaveTextContent('14.9 GB / 15.9 GB');
    });
    expect(screen.queryByTestId('fit-badge-unfit')).not.toBeInTheDocument();
  });

  it('falls back to backend verdict when additive baseline fields are absent', async () => {
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue([
      ...baseConfig,
      { key: 'llm.host-rtx5060.smart_num_ctx', value: 16384 },
    ]);
    vi.mocked(apiFetch).mockResolvedValue({
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
      expect(screen.getByTestId('fit-badge-partial')).toHaveTextContent('Partial offload · slower');
    });
    expect(screen.queryByText(/14\.9 GB \/ 15\.9 GB/)).not.toBeInTheDocument();
  });

  it('shows red badge and clamps slider when required VRAM exceeds 120%', async () => {
    /**
     * Produce "unfit": required > 0.120*15.9=19.08
     * Use the original fitDetailFits with kv=1_000_000:
     * At 16384: required = 12.0 + 8192 * 1_000_000 / 1e9 = 20.192 > 19.08 → unfit
     * At 8192: required = 12.0 → fits
     * Slider clamp should snap back to index 2 (8192)
     */
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail); // kv=1_000_000

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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);

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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);

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
    const { fetchConfig, apiFetch, setConfig } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);

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
        'llm.host-rtx5060.thinking_disabled.qwen3:14b',
        expect.any(Boolean),
      );
    });
  });
});

// ---------------------------------------------------------------------------
// Hardware Recommendation Banner (B3-2 — per-VRAM advisory)
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
      ...systemModelsWithFitDetail,
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
      ...systemModelsWithFitDetail,
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
      ...systemModelsWithFitDetail,
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
      ...systemModelsWithFitDetail,
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
      ...systemModelsWithFitDetail,
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue(systemModelsWithFitDetail);

    renderSection();

    await waitFor(() => {
      expect(screen.getByTestId('hardware-strip')).toBeInTheDocument();
    });

    expect(screen.queryByTestId('hw-recommendation-banner')).not.toBeInTheDocument();
  });
});

describe('IngestionSection — degraded backend (no fit_detail)', () => {
  it('renders without crashing when fit_detail is absent', async () => {
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
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
    const { fetchConfig, apiFetch } = await import('@/lib/api');
    vi.mocked(fetchConfig).mockResolvedValue(baseConfig);
    vi.mocked(apiFetch).mockResolvedValue({
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

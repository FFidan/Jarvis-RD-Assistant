import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AIPanel } from '@/components/settings/AIPanel';
import * as api from '@/lib/api';
import fs from 'node:fs';
import path from 'node:path';

vi.mock('@/lib/api');

// ── Structural contract: no inline literal keys ────────────────────────────
const PANEL_SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/settings/AIPanel.tsx'),
  'utf-8',
);

it('uses QUERY_KEYS.setup.firstRun() instead of literal ["setup-status"] for first-run status query', () => {
  expect(PANEL_SRC).not.toMatch(/\['setup-status'\]/);
  expect(PANEL_SRC).not.toMatch(/\["setup-status"\]/);
});

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => 'test-key'),
      logout: vi.fn(),
    })),
  },
}));

const wrap = (ui: React.ReactNode) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    {ui}
  </QueryClientProvider>
);

const baseSettings = {
  hw_tier: 'ge-48',
  recommended_backend: 'vllm',
  recommended_model: 'Qwen/Qwen3-14B-AWQ',
  configured_backend: 'vllm',
  configured_model: 'Qwen/Qwen3-14B-AWQ',
  observed_backend: 'vllm/Qwen/Qwen3-14B-AWQ',
  observed_recent_share: 1.0,
  candidates_for_tier: [
    { backend: 'vllm', model: 'Qwen/Qwen3-14B-AWQ', rank: 1, score: 105, reasoning: 'Top eval.' },
    { backend: 'vllm', model: 'Qwen/Qwen3-8B-AWQ', rank: 2, score: 63, reasoning: 'Strong reasoning.' },
  ],
  candidate_issues: [],
  eval_report_date: 'docs/perf/2026-05-22-tier-defaults-report.md',
};

/** Baseline first-run status (hw_tier_changed false — banner hidden). */
const baseSetupStatus = {
  configured: true,
  setup_mode: 'single' as const,
  hw_tier_changed: false,
};

describe('AIPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: no hw change, banner hidden
    vi.mocked(api.getFirstRunStatus).mockResolvedValue(baseSetupStatus as any);
    vi.mocked(api.dismissBanner).mockResolvedValue(undefined as any);
  });

  it('renders configured state and candidate dropdown', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    render(wrap(<AIPanel />));
    expect(await screen.findByText(/ge-48/)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Qwen3-14B-AWQ/ })).toBeInTheDocument();
  });

  it('renders plain backend names on the toggle buttons', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    render(wrap(<AIPanel />));
    await screen.findByText(/ge-48/);
    expect(screen.getByRole('button', { name: /vLLM \(high-throughput\)/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Ollama \(default\)/ })).toBeInTheDocument();
  });


  it('renders candidate evidence badges and vLLM local-route guidance', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue({
      ...baseSettings,
      candidates_for_tier: [
        {
          backend: 'vllm',
          model: 'Qwen/Qwen3-14B-AWQ',
          rank: 1,
          score: 105,
          reasoning: 'Top eval.',
          evidence: 'bench',
        },
        {
          backend: 'vllm',
          model: 'Qwen/Qwen3-8B-AWQ',
          rank: 2,
          reasoning: 'Awaiting target benchmark.',
          evidence: 'pending-bench',
        },
        {
          backend: 'ollama',
          model: 'qwen3:14b',
          rank: 1,
          reasoning: 'Catalog fallback.',
          source: 'catalog',
          evidence: 'catalog',
        },
      ],
    } as any);

    render(wrap(<AIPanel />));

    expect(await screen.findByText('Validated')).toBeInTheDocument();
    expect(screen.getByText('Needs validation')).toBeInTheDocument();
    expect(screen.getByText(/vLLM models must already be running behind the local LiteLLM route/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Ollama \(default\)/ }));
    expect(screen.getByText('Reference')).toBeInTheDocument();
  });

  it('guides the user instead of dead-ending when a backend has no candidates for the tier', async () => {
    // Recommended backend is ollama; only ollama has a candidate, so selecting
    // the vllm backend yields an empty model list → guidance, not a dead-end.
    vi.mocked(api.getAISettings).mockResolvedValue({
      ...baseSettings,
      recommended_backend: 'ollama',
      recommended_model: 'qwen3:1.7b',
      configured_backend: 'vllm',
      configured_model: 'Qwen/Qwen3-14B-AWQ',
      candidates_for_tier: [
        { backend: 'ollama', model: 'qwen3:1.7b', rank: 1, reasoning: 'Catalog fallback.' },
      ],
    } as any);
    render(wrap(<AIPanel />));

    // configured_backend=vllm is not selectable (no vllm candidate) so the panel
    // opens on the recommended ollama backend; click vllm to reach the empty state.
    const vllmButton = await screen.findByRole('button', { name: /vLLM \(high-throughput\)/ });
    fireEvent.click(vllmButton);

    const guidance = await screen.findByTestId('no-candidates-guidance');
    expect(guidance).toHaveTextContent(/no curated model for your hardware tier/i);
    expect(guidance).toHaveTextContent(/Ollama \(default\)/);
    expect(guidance).toHaveTextContent(/AI models page/i);
  });

  it('shows offline banner when observed != configured', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue({
      ...baseSettings,
      observed_backend: 'ollama/qwen3:1.7b',
      observed_recent_share: 0.94,
    } as any);
    render(wrap(<AIPanel />));
    expect(await screen.findByRole('alert')).toHaveTextContent(/offline/i);
  });


  it('renders configured observed recommended and catalog omission states', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue({
      ...baseSettings,
      recommended_backend: 'ollama',
      recommended_model: 'qwen3:72b',
      configured_backend: 'vllm',
      configured_model: 'Qwen/Qwen3-14B-AWQ',
      observed_backend: 'vllm/Qwen/Qwen3-14B-AWQ',
      candidates_for_tier: [
        { backend: 'ollama', model: 'qwen3:72b', rank: 1, reasoning: 'Catalog fallback.' },
      ],
      candidate_issues: [
        'ge-48 rank 1 vllm/Qwen/Qwen3-14B-AWQ: model is not in the curated model catalog',
      ],
    } as any);

    render(wrap(<AIPanel />));

    expect(await screen.findByTestId('candidate-issues')).toHaveTextContent(/curated model catalog/i);
    expect(screen.getByText('Configured').nextElementSibling!).toHaveTextContent(
      'vllm / Qwen/Qwen3-14B-AWQ',
    );
    expect(screen.getByText('Observed (recent)').nextElementSibling!).toHaveTextContent(
      'vllm/Qwen/Qwen3-14B-AWQ (100%)',
    );
    expect(screen.getAllByText('Recommended')[0]!.nextElementSibling!).toHaveTextContent(
      'ollama / qwen3:72b',
    );
    expect(screen.getByRole('option', { name: /qwen3:72b/ })).toBeInTheDocument();
  });

  it('shows hw-change banner when hw_tier_changed is true', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      hw_tier_changed: true,
      hw_tier_baseline: 'lt-24',
      hw_tier_current: 'ge-48',
    } as any);
    render(wrap(<AIPanel />));
    const banner = await screen.findByTestId('hw-change-banner');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent(/hardware tier has changed/i);
    expect(banner).toHaveTextContent(/lt-24/);
    expect(banner).toHaveTextContent(/ge-48/);
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument();
  });

  it('does not show hw-change banner when hw_tier_changed is false', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    // hw_tier_changed defaults to false in baseSetupStatus
    render(wrap(<AIPanel />));
    // Wait for the settings to load so queries settle
    await screen.findByText(/ge-48/);
    expect(screen.queryByTestId('hw-change-banner')).not.toBeInTheDocument();
  });

  it('shows gpu-cpu-mismatch banner and suppresses hw-change banner when GPU baseline runs on CPU', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      configured: true,
      hw_tier_baseline: '24-48',
      hw_tier_current: 'cpu',
      hw_tier_changed: true,
    } as any);
    render(wrap(<AIPanel />));
    expect(await screen.findByTestId('gpu-cpu-mismatch-banner')).toBeInTheDocument();
    expect(screen.queryByTestId('hw-change-banner')).toBeNull();
  });

  it('does not show gpu-cpu-mismatch banner when baseline and current tier match', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      hw_tier_baseline: '24-48',
      hw_tier_current: '24-48',
      hw_tier_changed: false,
    } as any);
    render(wrap(<AIPanel />));
    await screen.findByText(/ge-48/);
    expect(screen.queryByTestId('gpu-cpu-mismatch-banner')).not.toBeInTheDocument();
  });

  it('does not show gpu-cpu-mismatch banner when baseline is cpu', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      hw_tier_baseline: 'cpu',
      hw_tier_current: 'cpu',
      hw_tier_changed: false,
    } as any);
    render(wrap(<AIPanel />));
    await screen.findByText(/ge-48/);
    expect(screen.queryByTestId('gpu-cpu-mismatch-banner')).not.toBeInTheDocument();
  });
});

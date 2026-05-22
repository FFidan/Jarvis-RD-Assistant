import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SettingsAIPanel } from '@/pages/SettingsAIPanel';
import * as api from '@/lib/api';

vi.mock('@/lib/api');

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
  eval_report_date: 'docs/perf/2026-05-22-tier-defaults-report.md',
};

describe('SettingsAIPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders configured state and candidate dropdown', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    render(wrap(<SettingsAIPanel />));
    expect(await screen.findByText(/ge-48/)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Qwen3-14B-AWQ/ })).toBeInTheDocument();
  });

  it('shows offline banner when observed != configured', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue({
      ...baseSettings,
      observed_backend: 'ollama/qwen3:1.7b',
      observed_recent_share: 0.94,
    } as any);
    render(wrap(<SettingsAIPanel />));
    expect(await screen.findByRole('alert')).toHaveTextContent(/offline/i);
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { ModelDiagnosticsCard } from '@/components/admin/ModelDiagnosticsCard';
import * as api from '@/lib/api';
import { createTestQueryClient } from '@/__tests__/test-utils';

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
  <QueryClientProvider client={createTestQueryClient()}>
    {ui}
  </QueryClientProvider>
);

const baseSettings = {
  hw_tier: '24-48',
  recommended_backend: 'ollama',
  recommended_model: 'qwen3:14b',
  observed_backend: 'ollama/qwen3:14b',
  observed_recent_share: 0.92,
  candidates_for_tier: [{ backend: 'ollama', model: 'qwen3:14b', rank: 1 }],
  candidate_issues: [],
  eval_report_date: '2026-05-22',
};

describe('ModelDiagnosticsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.redetectHW).mockResolvedValue(baseSettings as any);
  });

  it('renders the detected tier, serving backend, and recommended model with its date', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    render(wrap(<ModelDiagnosticsCard />));

    expect(await screen.findByText('Model runtime')).toBeInTheDocument();
    expect(await screen.findByText('24-48')).toBeInTheDocument();
    expect(screen.getByTestId('observed-value')).toHaveTextContent('ollama/qwen3:14b (92%)');
    expect(screen.getByTestId('recommended-value')).toHaveTextContent('ollama / qwen3:14b');
    expect(screen.getByTestId('recommended-value')).toHaveTextContent('as of 2026-05-22');
  });

  it('surfaces excluded-candidate notes in a disclosure', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue({
      ...baseSettings,
      candidate_issues: [
        'ge-48 rank 1 vllm/Qwen/Qwen3-14B-AWQ: model is not in the curated model catalog',
      ],
    } as any);
    render(wrap(<ModelDiagnosticsCard />));

    expect(await screen.findByTestId('candidate-issues')).toHaveTextContent(/curated model catalog/i);
  });

  it('does not render operator-diagnostics chrome that moved elsewhere or was cut', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    render(wrap(<ModelDiagnosticsCard />));

    await screen.findByText('24-48');
    // Hardware alerts live on the model page now; evidence/guidance were cut.
    expect(screen.queryByTestId('hw-change-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('gpu-cpu-mismatch-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('candidate-evidence')).not.toBeInTheDocument();
    expect(screen.queryByTestId('backend-guidance')).not.toBeInTheDocument();
  });

  it('re-detects hardware on demand', async () => {
    vi.mocked(api.getAISettings).mockResolvedValue(baseSettings as any);
    render(wrap(<ModelDiagnosticsCard />));

    await screen.findByText('Model runtime');
    fireEvent.click(screen.getByRole('button', { name: /re-detect/i }));
    await waitFor(() => expect(api.redetectHW).toHaveBeenCalled());
  });
});

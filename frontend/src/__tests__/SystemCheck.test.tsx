/**
 * SystemCheck.test.tsx
 *
 * Verifies that model_warnings from SetupStatus are surfaced as warn Rows.
 * Mirrors the mocking style used in OnboardingWizard.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/lib/api', () => ({
  getSetupStatus: vi.fn(),
}));

const api = await import('@/lib/api');

const BASE_STATUS = {
  setup_completed: true,
  models_ready: true,
  models_downloading: [],
  topics_count: 1,
  telegram_configured: false,
  telegram_paired: false,
};

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}

describe('SystemCheck — model_warnings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a warn Row for each entry in model_warnings', async () => {
    const { SystemCheck } = await import('@/components/setup/SystemCheck');
    vi.mocked(api.getSetupStatus).mockResolvedValue({
      ...BASE_STATUS,
      model_warnings: ['smart routes to qwen3:8b which is not pulled'],
    });

    renderWithClient(<SystemCheck />);

    await waitFor(() => {
      expect(
        screen.getByText('smart routes to qwen3:8b which is not pulled'),
      ).toBeInTheDocument();
    });
  });

  it('renders no extra warn Rows when model_warnings is empty', async () => {
    const { SystemCheck } = await import('@/components/setup/SystemCheck');
    vi.mocked(api.getSetupStatus).mockResolvedValue({
      ...BASE_STATUS,
      model_warnings: [],
    });

    renderWithClient(<SystemCheck />);

    await waitFor(() => {
      expect(screen.getByText('API & database')).toBeInTheDocument();
    });

    expect(
      screen.queryByText('smart routes to qwen3:8b which is not pulled'),
    ).not.toBeInTheDocument();
  });

  it('renders no extra warn Rows when model_warnings is absent', async () => {
    const { SystemCheck } = await import('@/components/setup/SystemCheck');
    vi.mocked(api.getSetupStatus).mockResolvedValue(BASE_STATUS);

    renderWithClient(<SystemCheck />);

    await waitFor(() => {
      expect(screen.getByText('API & database')).toBeInTheDocument();
    });

    expect(
      screen.queryByText('smart routes to qwen3:8b which is not pulled'),
    ).not.toBeInTheDocument();
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { AIPanel } from '@/components/settings/AIPanel';
import * as api from '@/lib/api';
import { createTestQueryClient } from '@/__tests__/test-utils';

vi.mock('@/lib/api');

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      logout: vi.fn(),
    })),
  },
}));

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

const { toast } = await import('sonner');

const wrap = (ui: React.ReactNode) => (
  <QueryClientProvider client={createTestQueryClient()}>
    <MemoryRouter>{ui}</MemoryRouter>
  </QueryClientProvider>
);

const baseSetupStatus = {
  configured: true,
  setup_mode: 'single' as const,
  hw_tier_changed: false,
};

describe('AIPanel (model-page hardware alerts + pointer)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getFirstRunStatus).mockResolvedValue(baseSetupStatus as any);
    vi.mocked(api.dismissBanner).mockResolvedValue(undefined as any);
  });

  it('links to System Health for runtime diagnostics and shows no alert by default', async () => {
    render(wrap(<AIPanel />));

    const link = await screen.findByRole('link', { name: /system health/i });
    expect(link).toHaveAttribute('href', '/admin/system-health');
    // No operator-diagnostics chrome on the model page.
    expect(screen.queryByTestId('model-diagnostics')).not.toBeInTheDocument();
    expect(screen.queryByTestId('recommended-value')).not.toBeInTheDocument();
    expect(screen.queryByTestId('hw-change-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('gpu-cpu-mismatch-banner')).not.toBeInTheDocument();
  });

  it('shows the hardware-tier-changed alert and dismisses it', async () => {
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      hw_tier_changed: true,
      hw_tier_baseline: 'ge-48',
      hw_tier_current: '24-48',
    } as any);
    render(wrap(<AIPanel />));

    const banner = await screen.findByTestId('hw-change-banner');
    expect(banner).toHaveTextContent(/hardware tier has changed/i);
    expect(banner).toHaveTextContent(/ge-48/);
    expect(banner).toHaveTextContent(/24-48/);
    expect(banner).toHaveTextContent(/model cards above/i);

    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    await waitFor(() => expect(api.dismissBanner).toHaveBeenCalledWith('hw_change'));
  });

  it('shows a toast when dismissing the hardware-change notice is rejected by the server', async () => {
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      hw_tier_changed: true,
      hw_tier_baseline: 'ge-48',
      hw_tier_current: '24-48',
    } as any);
    vi.mocked(api.dismissBanner).mockRejectedValue(new Error(''));
    render(wrap(<AIPanel />));

    await screen.findByTestId('hw-change-banner');
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Could not dismiss this notice');
    });
  });

  it('shows the gpu-cpu-mismatch alert and suppresses the tier-changed alert', async () => {
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      hw_tier_baseline: '24-48',
      hw_tier_current: 'cpu',
      hw_tier_changed: true,
    } as any);
    render(wrap(<AIPanel />));

    expect(await screen.findByTestId('gpu-cpu-mismatch-banner')).toBeInTheDocument();
    expect(screen.queryByTestId('hw-change-banner')).toBeNull();
  });

  it('shows no gpu-cpu-mismatch alert when baseline and current tier match', async () => {
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({
      ...baseSetupStatus,
      hw_tier_baseline: '24-48',
      hw_tier_current: '24-48',
      hw_tier_changed: false,
    } as any);
    render(wrap(<AIPanel />));

    await screen.findByRole('link', { name: /system health/i });
    expect(screen.queryByTestId('gpu-cpu-mismatch-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('hw-change-banner')).not.toBeInTheDocument();
  });
});

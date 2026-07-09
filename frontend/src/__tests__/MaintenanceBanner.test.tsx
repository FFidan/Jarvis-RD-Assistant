import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MaintenanceBanner } from '@/components/shared/MaintenanceBanner';
import { useMaintenanceStore } from '@/stores/maintenance-store';

vi.mock('@/lib/api', () => ({
  fetchStackHealth: vi.fn(),
}));

import { fetchStackHealth } from '@/lib/api';
const mockFetchStackHealth = vi.mocked(fetchStackHealth);

const wrap = (ui: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
};

describe('MaintenanceBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMaintenanceStore.getState().clear();
  });

  it('renders nothing when maintenance is not active', () => {
    wrap(<MaintenanceBanner />);
    expect(screen.queryByTestId('maintenance-banner')).toBeNull();
    expect(mockFetchStackHealth).not.toHaveBeenCalled();
  });

  it('renders the banner when maintenance is active', () => {
    useMaintenanceStore.getState().setMaintenance(true, 30);
    mockFetchStackHealth.mockResolvedValue({
      services: [],
      degradedCount: 0,
      downCount: 0,
      overall: 'maintenance',
      maintenance: true,
    });
    wrap(<MaintenanceBanner />);
    expect(screen.getByTestId('maintenance-banner')).toBeTruthy();
  });

  it('clears the store and unmounts once the health payload reports maintenance is over', async () => {
    useMaintenanceStore.getState().setMaintenance(true, 30);
    mockFetchStackHealth.mockResolvedValue({
      services: [],
      degradedCount: 0,
      downCount: 0,
      overall: 'ok',
      maintenance: false,
    });
    wrap(<MaintenanceBanner />);

    await waitFor(() => expect(useMaintenanceStore.getState().active).toBe(false));
    await waitFor(() => expect(screen.queryByTestId('maintenance-banner')).toBeNull());
  });

  it('does NOT clear while the health payload still reports maintenance (exempt 200 is not the signal)', async () => {
    useMaintenanceStore.getState().setMaintenance(true, 30);
    mockFetchStackHealth.mockResolvedValue({
      services: [],
      degradedCount: 0,
      downCount: 0,
      overall: 'maintenance',
      maintenance: true,
    });
    wrap(<MaintenanceBanner />);

    await waitFor(() => expect(mockFetchStackHealth).toHaveBeenCalled());
    expect(useMaintenanceStore.getState().active).toBe(true);
    expect(screen.getByTestId('maintenance-banner')).toBeTruthy();
  });

  it('does NOT clear on an unknown/timeout summary (maintenance undefined is not a "restore over" signal)', async () => {
    // A probe timeout or a briefly-unreachable internal health endpoint yields
    // overall:'unknown' with maintenance undefined. The banner must stay and
    // keep polling — clearing on absence of a signal would flip-flop mid-restore.
    useMaintenanceStore.getState().setMaintenance(true, 30);
    mockFetchStackHealth.mockResolvedValue({
      services: [],
      degradedCount: 0,
      downCount: 0,
      overall: 'unknown',
      maintenance: undefined,
    });
    wrap(<MaintenanceBanner />);

    await waitFor(() => expect(mockFetchStackHealth).toHaveBeenCalled());
    expect(useMaintenanceStore.getState().active).toBe(true);
    expect(screen.getByTestId('maintenance-banner')).toBeTruthy();
  });
});

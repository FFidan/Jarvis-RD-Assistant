import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import { useMaintenanceStore } from '@/stores/maintenance-store';

describe('QueryErrorState', () => {
  beforeEach(() => {
    useMaintenanceStore.getState().clear();
  });

  it('renders the default connection-error copy when maintenance is not active', () => {
    render(<QueryErrorState />);
    expect(screen.getByText(/check your connection/i)).toBeTruthy();
  });

  it('renders the maintenance copy instead of the connection-error copy when maintenance is active', () => {
    useMaintenanceStore.getState().setMaintenance(true, 30);
    render(<QueryErrorState />);
    expect(
      screen.getByText(/temporarily read-only while a restore is running/i),
    ).toBeTruthy();
    expect(screen.queryByText(/check your connection/i)).toBeNull();
  });

  it('hides the Retry button during maintenance even when onRetry is provided', () => {
    useMaintenanceStore.getState().setMaintenance(true, 30);
    render(<QueryErrorState onRetry={() => {}} />);
    expect(screen.queryByText('Retry')).toBeNull();
  });

  it('still shows Retry for a genuine (non-maintenance) error when onRetry is provided', () => {
    render(<QueryErrorState onRetry={() => {}} />);
    expect(screen.getByText('Retry')).toBeTruthy();
  });
});

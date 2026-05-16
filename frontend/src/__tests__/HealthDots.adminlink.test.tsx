/**
 * HealthDots.adminlink.test.tsx — tests for the adminLink prop added in the
 * Shell/Sidebar+Admin IA redesign (spec §3.4).
 *
 * Covers:
 * - When adminLink is provided: renders a <Link> to that path, not a toggle button
 * - When adminLink is NOT provided: keeps existing toggle behavior (in-place expand)
 * - Non-admin in-place expand still works when no adminLink passed
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HealthDots } from '@/components/shared/HealthDots';
import type { StackHealthSummary } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchStackHealth: vi.fn(),
  };
});

import { fetchStackHealth } from '@/lib/api';
const mockFetchStackHealth = vi.mocked(fetchStackHealth);

function makeAllOk(): StackHealthSummary {
  return {
    overall: 'ok',
    degradedCount: 0,
    downCount: 0,
    services: [
      { name: 'paper_ingestion', label: 'Paper Ingestion', status: 'ok' },
      { name: 'learning_engine', label: 'Learning Engine', status: 'ok' },
    ],
  };
}

function renderHealthDots({ compact = false, adminLink }: { compact?: boolean; adminLink?: string } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HealthDots compact={compact} adminLink={adminLink} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('HealthDots — adminLink prop (admin navigation behavior)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders admin link anchor when adminLink is provided', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots({ adminLink: '/admin/system-health' });

    await waitFor(() => {
      expect(screen.getByTestId('health-pill-admin-link')).toBeInTheDocument();
    });

    const link = screen.getByTestId('health-pill-admin-link');
    expect(link.tagName).toBe('A');
    expect(link).toHaveAttribute('href', '/admin/system-health');
  });

  it('does NOT render the toggle button when adminLink is provided', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots({ adminLink: '/admin/system-health' });

    await waitFor(() => {
      expect(screen.getByTestId('health-pill-admin-link')).toBeInTheDocument();
    });

    expect(screen.queryByTestId('health-pill-toggle')).not.toBeInTheDocument();
  });

  it('shows health summary label in admin link mode', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots({ adminLink: '/admin/system-health' });

    await waitFor(() => {
      expect(screen.getByText('All healthy')).toBeInTheDocument();
    });
  });

  it('does NOT render admin link when adminLink is undefined (non-admin)', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    await waitFor(() => {
      expect(screen.getByTestId('health-pill-toggle')).toBeInTheDocument();
    });

    expect(screen.queryByTestId('health-pill-admin-link')).not.toBeInTheDocument();
  });

  it('toggle button still expands in-place when no adminLink (non-admin behavior)', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    const toggle = await screen.findByTestId('health-pill-toggle');
    fireEvent.click(toggle);

    expect(screen.getByTestId('health-expanded-grid')).toBeInTheDocument();
  });

  it('admin link accessible name includes health status', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots({ adminLink: '/admin/system-health' });

    await waitFor(() => {
      const link = screen.getByTestId('health-pill-admin-link');
      expect(link.getAttribute('aria-label')).toMatch(/All healthy/);
    });
  });
});

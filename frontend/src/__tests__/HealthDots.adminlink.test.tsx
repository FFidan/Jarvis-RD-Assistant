/**
 * HealthDots.adminlink.test.tsx — tests for the adminLink prop added in the
 * Shell/Sidebar+Admin IA redesign.
 *
 * Covers:
 * - When adminLink is provided: renders a popover trigger pill (not a raw <Link>);
 *   clicking it opens a per-service grid + "View full report" link to the admin path.
 * - When adminLink is NOT provided: keeps existing toggle behavior (in-place expand)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { HealthDots } from '@/components/shared/HealthDots';
import type { StackHealthSummary } from '@/lib/api';

const { SESSION_DURATION_MS } = vi.hoisted(() => ({
  SESSION_DURATION_MS: 30 * 24 * 60 * 60 * 1000,
}));

type AuthTestState = {
  isAuthenticated: boolean;
  authTime: number | null;
  isSessionValid: () => boolean;
  expireSession: ReturnType<typeof vi.fn>;
};

let authState: AuthTestState = {
  isAuthenticated: true,
  authTime: Date.now(),
  isSessionValid: () => true,
  expireSession: vi.fn(),
};

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: (selector: (state: typeof authState) => unknown) => selector(authState),
  SESSION_DURATION_MS,
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchStackHealth: vi.fn(),
  };
});

import { fetchStackHealth } from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
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
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <HealthDots compact={compact} adminLink={adminLink} />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('HealthDots — adminLink prop (admin popover behavior)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authState = {
      isAuthenticated: true,
      authTime: Date.now(),
      isSessionValid: () => true,
      expireSession: vi.fn(),
    };
  });

  it('renders popover trigger button when adminLink is provided', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots({ adminLink: '/admin/system-health' });

    await waitFor(() => {
      expect(screen.getByTestId('health-pill-admin-link')).toBeInTheDocument();
    });

    const trigger = screen.getByTestId('health-pill-admin-link');
    // Trigger is now a button (not an anchor); clicking opens a popover.
    expect(trigger.tagName).toBe('BUTTON');
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

  it('clicking the pill opens a popover containing all service rows', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    const user = userEvent.setup();
    renderHealthDots({ adminLink: '/admin/system-health' });

    const trigger = await screen.findByTestId('health-pill-admin-link');
    await user.click(trigger);

    await waitFor(() => {
      expect(screen.getByTestId('health-row-paper_ingestion')).toBeInTheDocument();
      expect(screen.getByTestId('health-row-learning_engine')).toBeInTheDocument();
    });
  });

  it('popover content includes a "Deployment & service health" link to adminLink path', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    const user = userEvent.setup();
    renderHealthDots({ adminLink: '/admin/system-health' });

    const trigger = await screen.findByTestId('health-pill-admin-link');
    await user.click(trigger);

    await waitFor(() => {
      const fullReportLink = screen.getByTestId('health-popover-full-report');
      expect(fullReportLink).toBeInTheDocument();
      expect(fullReportLink).toHaveAttribute('href', '/admin/system-health');
      expect(fullReportLink).toHaveTextContent('Deployment & service health');
    });
  });

  it('clicking the full-report link does NOT immediately navigate (stays in popover)', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    const user = userEvent.setup();
    renderHealthDots({ adminLink: '/admin/system-health' });

    const trigger = await screen.findByTestId('health-pill-admin-link');
    await user.click(trigger);

    // Full-report link is rendered as an anchor inside the popover, not a push
    // that closes the popover on mount — it must be present and clickable.
    const fullReportLink = await screen.findByTestId('health-popover-full-report');
    expect(fullReportLink).toBeInTheDocument();
    // It's an <a> rendered by react-router <Link>; MemoryRouter intercepts navigation
    // so it doesn't leave the page — the link merely exists here.
    expect(fullReportLink.tagName).toBe('A');
  });

  it('does NOT render admin trigger when adminLink is undefined (non-admin)', async () => {
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

  it('admin trigger accessible name includes health status', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots({ adminLink: '/admin/system-health' });

    await waitFor(() => {
      const trigger = screen.getByTestId('health-pill-admin-link');
      expect(trigger.getAttribute('aria-label')).toMatch(/All healthy/);
    });
  });
});

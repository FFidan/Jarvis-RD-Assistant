/**
 * Tests for AdminSystemHealthPage — per-check explanations + dev-mode context banner.
 *
 * Scope:
 * - Table renders all checks from the API.
 * - InfoTooltip appears next to a known check name and contains the mapped copy.
 * - Dev-mode banner is shown when any dev_* check is red or environment != green.
 * - Dev-mode banner is hidden when all checks are green.
 * - Unknown (future) check names render without a tooltip (graceful degradation).
 * - Loading and error states render correctly.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AdminSystemHealthPage } from '@/pages/AdminSystemHealthPage';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const getSystemReadinessMock = vi.fn();

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getSystemReadiness: () => getSystemReadinessMock(),
  };
});

// AdminBreadcrumb has no meaningful DOM output for these tests.
vi.mock('@/components/layout/AdminBreadcrumb', () => ({
  AdminBreadcrumb: ({ page }: { page: string }) =>
    React.createElement('nav', { 'aria-label': 'breadcrumb' }, page),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** All-green response — no dev flags active, environment is production. */
const allGreenResponse = {
  status: 'green' as const,
  checks: [
    { name: 'dev_auth_bypass', status: 'green' as const, detail: 'disabled' },
    { name: 'dev_error_detail', status: 'green' as const, detail: 'disabled' },
    { name: 'dev_cors_open', status: 'green' as const, detail: 'disabled' },
    { name: 'dev_smtp_log_only', status: 'green' as const, detail: 'disabled' },
    { name: 'dev_crypto_relaxed', status: 'green' as const, detail: 'disabled' },
    { name: 'environment', status: 'green' as const, detail: 'production' },
    { name: 'api_key', status: 'green' as const, detail: 'configured (>=32 chars)' },
    { name: 'smtp', status: 'green' as const, detail: 'configured' },
    { name: 'https', status: 'green' as const, detail: 'https' },
    { name: 'audit_log', status: 'green' as const, detail: '42 rows' },
  ],
};

/** Typical local-dev response — dev flags red, environment amber. */
const devModeResponse = {
  status: 'red' as const,
  checks: [
    { name: 'dev_auth_bypass', status: 'red' as const, detail: 'enabled' },
    { name: 'dev_error_detail', status: 'red' as const, detail: 'enabled' },
    { name: 'dev_cors_open', status: 'red' as const, detail: 'enabled' },
    { name: 'dev_smtp_log_only', status: 'red' as const, detail: 'enabled' },
    { name: 'dev_crypto_relaxed', status: 'red' as const, detail: 'enabled' },
    { name: 'environment', status: 'amber' as const, detail: 'development' },
    { name: 'api_key', status: 'green' as const, detail: 'configured (>=32 chars)' },
    { name: 'smtp', status: 'amber' as const, detail: 'not configured — magic links go to stdout' },
    { name: 'https', status: 'amber' as const, detail: 'http' },
    { name: 'audit_log', status: 'green' as const, detail: '0 rows' },
  ],
};

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AdminSystemHealthPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AdminSystemHealthPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders all checks from the API in the table', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText('dev_auth_bypass')).toBeInTheDocument();
    });

    // All 10 check names must be present in the table (use getAllByText because
    // Radix tooltip portals may render hidden a11y copies of some strings).
    for (const check of allGreenResponse.checks) {
      const matches = screen.getAllByText(check.name);
      expect(matches.length).toBeGreaterThan(0);
    }
  });

  it('shows loading state initially', () => {
    getSystemReadinessMock.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(screen.getByText(/loading system health/i)).toBeInTheDocument();
  });

  it('shows error state when getSystemReadiness fails', async () => {
    getSystemReadinessMock.mockRejectedValueOnce(new Error('network error'));
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/failed to load system health/i)).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Tooltip tests
  // -------------------------------------------------------------------------

  it('renders an info tooltip for dev_auth_bypass containing the explanation copy', async () => {
    const user = userEvent.setup();
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => screen.getByText('dev_auth_bypass'));

    // The InfoTooltip renders a button with aria-label "More info".
    // Multiple tooltips exist (one per known check); hover the first one that
    // is a sibling of dev_auth_bypass. We find it via the table row.
    const authBypassCell = screen.getByText('dev_auth_bypass').closest('td')!;
    const tooltipTrigger = authBypassCell.querySelector('[aria-label="More info"]')!;
    expect(tooltipTrigger).toBeInTheDocument();

    await user.hover(tooltipTrigger);

    await waitFor(() => {
      const matches = screen.getAllByText(/Development auth bypass is ON/i);
      expect(matches.length).toBeGreaterThan(0);
    });
  });

  it('renders an info tooltip for smtp containing the explanation copy', async () => {
    const user = userEvent.setup();
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => screen.getByText('smtp'));

    const smtpCell = screen.getByText('smtp').closest('td')!;
    const tooltipTrigger = smtpCell.querySelector('[aria-label="More info"]')!;
    expect(tooltipTrigger).toBeInTheDocument();

    await user.hover(tooltipTrigger);

    await waitFor(() => {
      const matches = screen.getAllByText(/magic-link sign-in emails/i);
      expect(matches.length).toBeGreaterThan(0);
    });
  });

  it('does not render a tooltip for an unknown future check name', async () => {
    const responseWithUnknown = {
      status: 'amber' as const,
      checks: [
        { name: 'future_unknown_check', status: 'amber' as const, detail: 'some detail' },
      ],
    };
    getSystemReadinessMock.mockResolvedValueOnce(responseWithUnknown);
    renderPage();

    await waitFor(() => screen.getByText('future_unknown_check'));

    const cell = screen.getByText('future_unknown_check').closest('td')!;
    // No tooltip trigger inside the unknown check's cell.
    expect(cell.querySelector('[aria-label="More info"]')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Dev-mode banner
  // -------------------------------------------------------------------------

  it('shows the dev-mode banner when a dev_* check is red', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText(/This instance is running in development mode/i),
      ).toBeInTheDocument();
    });
  });

  it('shows the dev-mode banner when environment is amber (not production)', async () => {
    const envAmberResponse = {
      status: 'amber' as const,
      checks: [
        { name: 'dev_auth_bypass', status: 'green' as const, detail: 'disabled' },
        { name: 'environment', status: 'amber' as const, detail: 'staging' },
        { name: 'api_key', status: 'green' as const, detail: 'configured (>=32 chars)' },
      ],
    };
    getSystemReadinessMock.mockResolvedValueOnce(envAmberResponse);
    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText(/This instance is running in development mode/i),
      ).toBeInTheDocument();
    });
  });

  it('hides the dev-mode banner when all checks are green (aggregate green response)', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => screen.getByText('dev_auth_bypass'));

    expect(
      screen.queryByText(/This instance is running in development mode/i),
    ).not.toBeInTheDocument();
  });

  it('dev-mode banner has role="alert" for accessibility', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      const banner = screen.getByRole('alert');
      expect(banner).toHaveTextContent(/running in development mode/i);
    });
  });
});

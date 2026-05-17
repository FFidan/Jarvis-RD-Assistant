/**
 * Tests for AdminSystemHealthPage — per-check explanations + dev-mode context banner
 * + live-services section (fetchStackHealth) + Vector clarity.
 *
 * Scope:
 * - Table renders all checks from the API.
 * - InfoTooltip appears next to a known check name and contains the mapped copy.
 * - Dev-mode banner is shown when any dev_* check is red or environment != green.
 * - Dev-mode banner is hidden when all checks are green.
 * - Unknown (future) check names render without a tooltip (graceful degradation).
 * - Loading and error states render correctly.
 * - Live services section renders per-service rows from fetchStackHealth.
 * - Vector shown as "Log collector (optional)" with note when unknown.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AdminSystemHealthPage } from '@/pages/AdminSystemHealthPage';
import type { StackHealthSummary } from '@/lib/api';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const getSystemReadinessMock = vi.fn();
const fetchStackHealthMock = vi.fn();

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getSystemReadiness: () => getSystemReadinessMock(),
    fetchStackHealth: () => fetchStackHealthMock(),
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

/**
 * Default stack health fixture.
 * @param vectorOk - when true vector is 'ok', otherwise 'unknown' (default).
 */
function makeStackHealth(vectorOk = false): StackHealthSummary {
  return {
    overall: 'ok',
    degradedCount: 0,
    downCount: 0,
    services: [
      { name: 'paper_ingestion', label: 'Paper Ingestion', status: 'ok' },
      { name: 'learning_engine', label: 'Learning Engine', status: 'ok' },
      { name: 'postgres', label: 'PostgreSQL', status: 'ok' },
      { name: 'qdrant', label: 'Qdrant', status: 'ok' },
      { name: 'ollama', label: 'Ollama', status: 'ok' },
      { name: 'litellm', label: 'LiteLLM', status: 'ok' },
      { name: 'vector', label: 'Vector', status: vectorOk ? 'ok' : 'unknown' },
    ],
  };
}

/** All-green response — no dev flags active, environment is production. */
const allGreenResponse = {
  status: 'green' as const,
  checks: [
    {
      name: 'dev_auth_bypass',
      status: 'green' as const,
      detail: 'disabled',
      remediation: 'Set DEV_AUTH_BYPASS=false and DEV_MODE=false before sharing this URL.',
    },
    {
      name: 'dev_error_detail',
      status: 'green' as const,
      detail: 'disabled',
      remediation: 'Set DEV_ERROR_DETAIL=false in production.',
    },
    {
      name: 'dev_cors_open',
      status: 'green' as const,
      detail: 'disabled',
      remediation: 'Set DEV_CORS_OPEN=false and restrict CORS_ORIGINS to your domain.',
    },
    {
      name: 'dev_smtp_log_only',
      status: 'green' as const,
      detail: 'disabled',
      remediation: 'Set DEV_SMTP_LOG_ONLY=false and configure SMTP credentials.',
    },
    {
      name: 'dev_crypto_relaxed',
      status: 'green' as const,
      detail: 'disabled',
      remediation: 'Set DEV_CRYPTO_RELAXED=false in production.',
    },
    {
      name: 'environment',
      status: 'green' as const,
      detail: 'production',
      remediation: 'Set ENVIRONMENT=production before going live.',
    },
    {
      name: 'api_key',
      status: 'green' as const,
      detail: 'configured (>=32 chars)',
      remediation: '',
    },
    {
      name: 'smtp',
      status: 'green' as const,
      detail: 'configured',
      remediation: 'Configure SMTP_HOST, SMTP_USER, SMTP_PASS, and SMTP_FROM.',
    },
    {
      name: 'https',
      status: 'green' as const,
      detail: 'https',
      remediation: 'Ensure TLS is terminated at the edge.',
    },
    {
      name: 'audit_log',
      status: 'green' as const,
      detail: '42 rows',
      remediation: '',
    },
  ],
};

/** Typical local-dev response — dev flags red, environment amber. */
const devModeResponse = {
  status: 'red' as const,
  checks: [
    {
      name: 'dev_auth_bypass',
      status: 'red' as const,
      detail: 'enabled',
      remediation: 'Set DEV_AUTH_BYPASS=false and DEV_MODE=false before sharing this URL.',
    },
    {
      name: 'dev_error_detail',
      status: 'red' as const,
      detail: 'enabled',
      remediation: 'Set DEV_ERROR_DETAIL=false in production.',
    },
    {
      name: 'dev_cors_open',
      status: 'red' as const,
      detail: 'enabled',
      remediation: 'Set DEV_CORS_OPEN=false and restrict CORS_ORIGINS to your domain.',
    },
    {
      name: 'dev_smtp_log_only',
      status: 'red' as const,
      detail: 'enabled',
      remediation: 'Set DEV_SMTP_LOG_ONLY=false and configure SMTP credentials.',
    },
    {
      name: 'dev_crypto_relaxed',
      status: 'red' as const,
      detail: 'enabled',
      remediation: 'Set DEV_CRYPTO_RELAXED=false in production.',
    },
    {
      name: 'environment',
      status: 'amber' as const,
      detail: 'development',
      remediation: 'Set ENVIRONMENT=production before going live.',
    },
    {
      name: 'api_key',
      status: 'green' as const,
      detail: 'configured (>=32 chars)',
      remediation: '',
    },
    {
      name: 'smtp',
      status: 'amber' as const,
      detail: 'not configured — magic links go to stdout',
      remediation: 'Configure SMTP_HOST, SMTP_USER, SMTP_PASS, and SMTP_FROM.',
    },
    {
      name: 'https',
      status: 'amber' as const,
      detail: 'http',
      remediation: 'Ensure TLS is terminated at the edge.',
    },
    {
      name: 'audit_log',
      status: 'green' as const,
      detail: '0 rows',
      remediation: '',
    },
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
    // Default stack health mock — vector unknown (normal)
    fetchStackHealthMock.mockResolvedValue(makeStackHealth());
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
    expect(screen.getByText(/loading readiness checks/i)).toBeInTheDocument();
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
      const matches = screen.getAllByText(/Security bypass that allows unrestricted sign-in/i);
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
      const matches = screen.getAllByText(/Email delivery configuration/i);
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

  // -------------------------------------------------------------------------
  // Live services section
  // -------------------------------------------------------------------------

  it('renders the live services section with per-service rows', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    // Wait for the table to appear (fetchStackHealth resolves)
    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });

    // All 7 services should be present as rows
    const expectedNames = [
      'paper_ingestion',
      'learning_engine',
      'postgres',
      'qdrant',
      'ollama',
      'litellm',
      'vector',
    ];
    for (const name of expectedNames) {
      expect(screen.getByTestId(`live-svc-row-${name}`)).toBeInTheDocument();
    }
  });

  it('shows readiness checks alongside live services (superset)', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    // Both sections must resolve and be visible simultaneously
    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText('dev_auth_bypass')).toBeInTheDocument();
    });
    expect(screen.getByTestId('live-services-section')).toBeInTheDocument();
  });

  it('Vector service shows "Log collector (optional)" label', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });

    const vectorRow = screen.getByTestId('live-svc-row-vector');
    expect(vectorRow).toHaveTextContent('Log collector (optional)');
    // Raw backend label must not appear as standalone text
    expect(vectorRow.querySelector('td')?.textContent).toBe('Log collector (optional)');
  });

  it('Vector row shows plain-language note when status is unknown', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    // beforeEach sets fetchStackHealthMock to makeStackHealth() — vector is unknown
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-svc-row-vector')).toBeInTheDocument();
    });

    const vectorRow = screen.getByTestId('live-svc-row-vector');
    expect(vectorRow).toHaveTextContent(/Optional log shipper/i);
    expect(vectorRow).toHaveTextContent(/observability/i);
  });

  it('Vector row shows no note when status is ok', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    // Override: vector is ok
    fetchStackHealthMock.mockResolvedValue(makeStackHealth(/* vectorOk= */ true));
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-svc-row-vector')).toBeInTheDocument();
    });

    const vectorRow = screen.getByTestId('live-svc-row-vector');
    expect(vectorRow).not.toHaveTextContent(/Optional log shipper/i);
  });

  // -------------------------------------------------------------------------
  // Per-check remediation rendering
  // -------------------------------------------------------------------------

  it('renders remediation text for dev_auth_bypass when red', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Set DEV_AUTH_BYPASS=false/i)).toBeInTheDocument();
    });
  });

  it('renders remediation text for dev_error_detail when red', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Set DEV_ERROR_DETAIL=false/i)).toBeInTheDocument();
    });
  });

  it('renders remediation text for dev_cors_open when red', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Set DEV_CORS_OPEN=false/i)).toBeInTheDocument();
    });
  });

  it('renders remediation text for dev_smtp_log_only when red', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Set DEV_SMTP_LOG_ONLY=false/i)).toBeInTheDocument();
    });
  });

  it('renders remediation text for dev_crypto_relaxed when red', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Set DEV_CRYPTO_RELAXED=false/i)).toBeInTheDocument();
    });
  });

  it('renders remediation text for environment when amber', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Set ENVIRONMENT=production before going live/i)).toBeInTheDocument();
    });
  });

  it('does not render remediation text when it is empty', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText('api_key')).toBeInTheDocument();
    });

    // api_key has empty remediation; should not appear in that row's detail section
    const apiKeyRow = screen
      .getByText('api_key')
      .closest('tr');
    // The detail cell should show only "configured (>=32 chars)"
    const detailCell = apiKeyRow?.querySelector('td:nth-child(3)');
    expect(detailCell?.textContent).toContain('configured (>=32 chars)');
    // No remediation text should be present (would be in orange-600 color if rendered)
    expect(detailCell?.querySelector('.text-orange-600')).toBeNull();
  });
});

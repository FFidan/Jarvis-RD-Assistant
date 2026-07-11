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
import { QUERY_KEYS } from '@/lib/query-keys';
import type { StackHealthSummary } from '@/lib/api';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const getSystemReadinessMock = vi.fn();
const fetchStackHealthMock = vi.fn();
const getSystemStorageMock = vi.fn();

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getSystemReadiness: () => getSystemReadinessMock(),
    fetchStackHealth: () => fetchStackHealthMock(),
    getSystemStorage: () => getSystemStorageMock(),
    // ModelDiagnosticsCard is mounted on this page; give it controlled data so
    // it renders without hitting the network.
    getAISettings: () =>
      Promise.resolve({
        hw_tier: 'cpu',
        recommended_backend: 'ollama',
        recommended_model: 'qwen3:1.7b',
        observed_backend: null,
        observed_recent_share: 0,
        candidates_for_tier: [],
        candidate_issues: [],
        eval_report_date: null,
      }),
    getFirstRunStatus: () => Promise.resolve({ configured: true, hw_tier_changed: false }),
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

/**
 * The synthesized degraded summary fetchStackHealth resolves to when the health
 * probes don't respond within the deadline: every service 'unknown',
 * overall 'unknown'.
 */
function makeAllUnknownStackHealth(): StackHealthSummary {
  return {
    overall: 'unknown',
    degradedCount: 0,
    downCount: 0,
    services: [
      { name: 'paper_ingestion', label: 'Paper Ingestion', status: 'unknown' },
      { name: 'learning_engine', label: 'Learning Engine', status: 'unknown' },
      { name: 'postgres', label: 'PostgreSQL', status: 'unknown' },
      { name: 'qdrant', label: 'Qdrant', status: 'unknown' },
      { name: 'ollama', label: 'Ollama', status: 'unknown' },
      { name: 'litellm', label: 'LiteLLM', status: 'unknown' },
      { name: 'vector', label: 'Vector', status: 'unknown' },
    ],
  };
}

/** Default storage snapshot — every backend reachable, no pressure. */
function makeStorageResponse(overrides: Partial<import('@/lib/api').SystemStorageResponse> = {}) {
  return {
    ollama_models: { bytes_used: 9_200_000_000, error: null },
    postgres: { bytes_used: 512_000_000, error: null },
    qdrant: { bytes_used: null, error: null },
    qdrant_collections: [{ name: 'papers', points_count: 1200 }],
    hf_cache: { bytes_used: 1_500_000_000, error: null },
    pressure: false,
    ...overrides,
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
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <AdminSystemHealthPage />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AdminSystemHealthPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default stack health mock — vector unknown (normal)
    fetchStackHealthMock.mockResolvedValue(makeStackHealth());
    getSystemStorageMock.mockResolvedValue(makeStorageResponse());
  });

  it('renders all checks from the API in the table using display labels', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    // Display labels (not raw snake_case names) should be visible
    await waitFor(() => {
      expect(screen.getByText('Auth bypass (dev)')).toBeInTheDocument();
    });

    const expectedLabels = [
      'Auth bypass (dev)',
      'Error detail (dev)',
      'Open CORS (dev)',
      'SMTP log-only (dev)',
      'Relaxed crypto (dev)',
      'Environment',
      'API key',
      'Email delivery (SMTP)',
      'HTTPS / TLS',
      'Audit log',
    ];
    for (const label of expectedLabels) {
      const matches = screen.getAllByText(label);
      expect(matches.length).toBeGreaterThan(0);
    }

    // Raw snake_case names must NOT be primary visible text
    expect(screen.queryByText('dev_auth_bypass')).not.toBeInTheDocument();
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

  it('renders an info tooltip for auth-bypass check containing the explanation copy', async () => {
    const user = userEvent.setup();
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => screen.getByText('Auth bypass (dev)'));

    // The InfoTooltip renders a button with aria-label "More info".
    // Multiple tooltips exist (one per known check); hover the one in the auth bypass row.
    const authBypassCell = screen.getByText('Auth bypass (dev)').closest('td')!;
    const tooltipTrigger = authBypassCell.querySelector('[aria-label="More info"]')!;
    expect(tooltipTrigger).toBeInTheDocument();

    await user.hover(tooltipTrigger);

    await waitFor(() => {
      const matches = screen.getAllByText(/Security bypass that allows unrestricted sign-in/i);
      expect(matches.length).toBeGreaterThan(0);
    });
  });

  it('renders an info tooltip for SMTP check containing the explanation copy', async () => {
    const user = userEvent.setup();
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => screen.getByText('Email delivery (SMTP)'));

    const smtpCell = screen.getByText('Email delivery (SMTP)').closest('td')!;
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

    // Unknown check name has no display label, so the raw name is shown
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

    await waitFor(() => screen.getByText('Auth bypass (dev)'));

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

  it('reads live services from the shared stack-health cache key (same entry as HealthDots)', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    const { queryClient } = renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });

    // The live-services data lives under the ONE shared key — the sidebar pill
    // and this page can never render contradictory snapshots.
    expect(queryClient.getQueryData(QUERY_KEYS.stack.health())).toEqual(makeStackHealth());
    expect(queryClient.getQueryData(['admin', 'stack-health'])).toBeUndefined();
  });

  it('settles live services to "Unknown" badges (not stuck "Checking services…") when the probe times out', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    // fetchStackHealth applies its own hard deadline and resolves to an
    // all-unknown summary when the probes hang; simulate that resolved value.
    fetchStackHealthMock.mockResolvedValue(makeAllUnknownStackHealth());
    renderPage();

    // The table renders (we left the "Checking…" state) ...
    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });
    expect(screen.queryByText('Checking services…')).not.toBeInTheDocument();

    // ... every service row shows an "Unknown" badge.
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
      const row = screen.getByTestId(`live-svc-row-${name}`);
      expect(row.querySelector('[data-testid="svc-status-badge-unknown"]')).toBeInTheDocument();
    }

    // The summary reflects the no-response state rather than "All services running."
    const summary = screen.getByTestId('stack-summary');
    expect(summary).toHaveTextContent(/did not respond in time/i);
    expect(summary).not.toHaveTextContent(/All services running/i);
  });

  it('shows readiness checks alongside live services (superset)', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    // Both sections must resolve and be visible simultaneously
    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText('Auth bypass (dev)')).toBeInTheDocument();
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
  // Storage card
  // -------------------------------------------------------------------------

  it('renders the storage card with per-store disk usage', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('storage-card')).toHaveTextContent('9.2 GB');
    });
    expect(screen.getByTestId('storage-card')).toHaveTextContent('1,200 points across 1 collection');
  });

  it('shows a low-disk notice when the storage endpoint reports pressure', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    getSystemStorageMock.mockResolvedValue(makeStorageResponse({ pressure: true }));
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/running low/i)).toBeInTheDocument();
    });
  });

  it('shows an unavailable state for a backing store that could not be measured, without failing the others', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    getSystemStorageMock.mockResolvedValue(
      makeStorageResponse({
        ollama_models: { bytes_used: null, error: 'ConnectError' },
        qdrant: { bytes_used: null, error: 'ConnectError' },
        qdrant_collections: [],
      }),
    );
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('storage-card')).toHaveTextContent('Unavailable (ConnectError)');
    });
    // The reachable sections still render their figures.
    expect(screen.getByTestId('storage-card')).toHaveTextContent('512 MB');
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

    // api_key renders as "API key" display label
    await waitFor(() => {
      expect(screen.getByText('API key')).toBeInTheDocument();
    });

    // API key check has empty remediation; should not appear in that row's detail section
    const apiKeyRow = screen
      .getByText('API key')
      .closest('tr');
    // The detail cell should show only "configured (>=32 chars)"
    const detailCell = apiKeyRow?.querySelector('td:nth-child(3)');
    expect(detailCell?.textContent).toContain('configured (>=32 chars)');
    // No remediation text should be present (would be in orange-600 color if rendered)
    expect(detailCell?.querySelector('.text-orange-600')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // StatusBadge verdict words (H1)
  // -------------------------------------------------------------------------

  it('overall status badge shows "Ready" when status is green', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getAllByText('Ready').length).toBeGreaterThan(0);
    });
    expect(screen.queryByText('green')).not.toBeInTheDocument();
  });

  it('overall status badge shows "Action required" when status is red', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(devModeResponse);
    renderPage();

    await waitFor(() => {
      // devModeResponse status is 'red' — multiple per-check badges also show "Action required"
      expect(screen.getAllByText('Action required').length).toBeGreaterThan(0);
    });
    expect(screen.queryByText('red')).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Service display labels (H2)
  // -------------------------------------------------------------------------

  it('Qdrant service shows "Search index (Qdrant)" label', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });

    const qdrantRow = screen.getByTestId('live-svc-row-qdrant');
    expect(qdrantRow.querySelector('td')?.textContent).toBe('Search index (Qdrant)');
  });

  it('LiteLLM service shows "AI model router (LiteLLM)" label', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-services-table')).toBeInTheDocument();
    });

    const litellmRow = screen.getByTestId('live-svc-row-litellm');
    expect(litellmRow.querySelector('td')?.textContent).toBe('AI model router (LiteLLM)');
  });

  // -------------------------------------------------------------------------
  // Consequence notes for down/degraded services (H2)
  // -------------------------------------------------------------------------

  it('shows consequence note for Qdrant when down', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    fetchStackHealthMock.mockResolvedValue({
      ...makeStackHealth(),
      services: makeStackHealth().services.map((s) =>
        s.name === 'qdrant' ? { ...s, status: 'down' as const } : s,
      ),
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-svc-row-qdrant')).toBeInTheDocument();
    });

    const qdrantRow = screen.getByTestId('live-svc-row-qdrant');
    expect(qdrantRow).toHaveTextContent(/Semantic search and citation graph are unavailable/i);
  });

  it('shows consequence note for LiteLLM when degraded', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    fetchStackHealthMock.mockResolvedValue({
      ...makeStackHealth(),
      services: makeStackHealth().services.map((s) =>
        s.name === 'litellm' ? { ...s, status: 'degraded' as const } : s,
      ),
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('live-svc-row-litellm')).toBeInTheDocument();
    });

    const litellmRow = screen.getByTestId('live-svc-row-litellm');
    expect(litellmRow).toHaveTextContent(/AI-powered features.*unavailable/i);
  });

  // -------------------------------------------------------------------------
  // Overall stack summary (H2)
  // -------------------------------------------------------------------------

  it('shows "All services running" summary when all services are ok', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    fetchStackHealthMock.mockResolvedValue(makeStackHealth(/* vectorOk= */ true));
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('stack-summary')).toBeInTheDocument();
    });

    expect(screen.getByTestId('stack-summary')).toHaveTextContent('All services running.');
  });

  it('shows down count in summary when a service is down', async () => {
    getSystemReadinessMock.mockResolvedValueOnce(allGreenResponse);
    fetchStackHealthMock.mockResolvedValue({
      overall: 'down' as const,
      downCount: 1,
      degradedCount: 0,
      services: makeStackHealth().services.map((s) =>
        s.name === 'postgres' ? { ...s, status: 'down' as const } : s,
      ),
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('stack-summary')).toBeInTheDocument();
    });

    expect(screen.getByTestId('stack-summary')).toHaveTextContent(/1 service down/i);
  });
});

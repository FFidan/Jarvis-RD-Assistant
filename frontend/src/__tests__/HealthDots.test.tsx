/**
 * Tests for the HealthDots component (Bucket D3).
 *
 * Covers:
 * - Collapsed pill: "All healthy" / "N degraded" / "N down" labels
 * - Click-to-expand reveals per-service grid
 * - Compact mode (sidebar-collapsed) renders dot row
 * - Loading / error states
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { HealthDots } from '@/components/shared/HealthDots';
import { QUERY_KEYS } from '@/lib/query-keys';
import type { StackHealthSummary } from '@/lib/api';

const SESSION_DURATION_MS = 8 * 60 * 60 * 1000;

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
}));

// Mock the api module so we control fetchStackHealth return values
vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchStackHealth: vi.fn(),
  };
});

import { fetchStackHealth } from '@/lib/api';
const mockFetchStackHealth = vi.mocked(fetchStackHealth);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeAllOk(): StackHealthSummary {
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
      { name: 'vector', label: 'Vector', status: 'unknown' },
    ],
  };
}

function makeWithDown(count: number): StackHealthSummary {
  const base = makeAllOk();
  const services = base.services.map((s, i) =>
    i < count ? { ...s, status: 'down' as const } : s,
  );
  return {
    ...base,
    overall: 'down',
    downCount: count,
    degradedCount: 0,
    services,
  };
}

/**
 * The synthesized degraded summary fetchStackHealth resolves to when the health
 * probes don't respond within the deadline: every service 'unknown', overall
 * 'unknown'. The UI must leave the "Checking…" state and render unknown dots.
 */
function makeAllUnknown(): StackHealthSummary {
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

function makeWithDegraded(count: number): StackHealthSummary {
  const base = makeAllOk();
  const services = base.services.map((s, i) =>
    i < count ? { ...s, status: 'degraded' as const } : s,
  );
  return {
    ...base,
    overall: 'degraded',
    downCount: 0,
    degradedCount: count,
    services,
  };
}

function renderHealthDots(compact = false) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <HealthDots compact={compact} />
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('HealthDots', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authState = {
      isAuthenticated: true,
      authTime: Date.now(),
      isSessionValid: () => true,
      expireSession: vi.fn(),
    };
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not poll protected health endpoints before authentication', () => {
    authState = {
      isAuthenticated: false,
      authTime: null,
      isSessionValid: () => false,
      expireSession: vi.fn(),
    };
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    expect(mockFetchStackHealth).not.toHaveBeenCalled();
    expect(screen.getByTestId('health-dots-loading')).toBeInTheDocument();
  });



  it('hides cached protected health data when authentication is no longer valid', () => {
    authState = {
      isAuthenticated: false,
      authTime: null,
      isSessionValid: () => false,
      expireSession: vi.fn(),
    };
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData(QUERY_KEYS.stack.health(), makeAllOk());

    render(
      <QueryClientProvider client={queryClient}>
        <HealthDots />
      </QueryClientProvider>,
    );

    expect(mockFetchStackHealth).not.toHaveBeenCalled();
    expect(screen.getByTestId('health-dots-loading')).toBeInTheDocument();
    expect(screen.queryByText('All healthy')).not.toBeInTheDocument();
  });

  it('does not poll protected health when the client session is already expired', () => {
    const expireSession = vi.fn();
    authState = {
      isAuthenticated: true,
      authTime: Date.now() - SESSION_DURATION_MS - 1,
      isSessionValid: () => false,
      expireSession,
    };
    mockFetchStackHealth.mockResolvedValue(makeAllOk());

    renderHealthDots();

    expect(mockFetchStackHealth).not.toHaveBeenCalled();
    expect(expireSession).not.toHaveBeenCalled();
    expect(screen.getByTestId('health-dots-loading')).toBeInTheDocument();
  });

  // --- Collapsed pill ---

  it('shows "All healthy" pill when all services are ok', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    await waitFor(() => {
      expect(screen.getByTestId('health-pill-toggle')).toBeInTheDocument();
    });

    expect(screen.getByText('All healthy')).toBeInTheDocument();
  });

  it('shows "N down" pill when services are down', async () => {
    mockFetchStackHealth.mockResolvedValue(makeWithDown(2));
    renderHealthDots();

    await waitFor(() => {
      expect(screen.getByText('2 down')).toBeInTheDocument();
    });
  });

  it('shows "N degraded" pill when services are degraded', async () => {
    mockFetchStackHealth.mockResolvedValue(makeWithDegraded(3));
    renderHealthDots();

    await waitFor(() => {
      expect(screen.getByText('3 degraded')).toBeInTheDocument();
    });
  });

  // --- Expand / collapse ---

  it('expanded grid is hidden by default', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    await waitFor(() => screen.getByTestId('health-pill-toggle'));

    expect(screen.queryByTestId('health-expanded-grid')).not.toBeInTheDocument();
  });

  it('clicking the pill toggles the expanded grid', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    const toggle = await screen.findByTestId('health-pill-toggle');

    // First click — expand
    fireEvent.click(toggle);
    expect(screen.getByTestId('health-expanded-grid')).toBeInTheDocument();

    // Second click — collapse
    fireEvent.click(toggle);
    expect(screen.queryByTestId('health-expanded-grid')).not.toBeInTheDocument();
  });

  it('expanded grid contains all 7 service rows', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    const toggle = await screen.findByTestId('health-pill-toggle');
    fireEvent.click(toggle);

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
      expect(screen.getByTestId(`health-row-${name}`)).toBeInTheDocument();
    }
  });

  it('expanded grid shows service labels', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots();

    const toggle = await screen.findByTestId('health-pill-toggle');
    fireEvent.click(toggle);

    expect(screen.getByText('Paper Ingestion')).toBeInTheDocument();
    expect(screen.getByText('Learning Engine')).toBeInTheDocument();
    expect(screen.getByText('PostgreSQL')).toBeInTheDocument();
    expect(screen.getByText('Qdrant')).toBeInTheDocument();
    expect(screen.getByText('Ollama')).toBeInTheDocument();
    expect(screen.getByText('LiteLLM')).toBeInTheDocument();
    // Vector is relabelled to the self-hoster-friendly display name
    expect(screen.getByText('Log collector (optional)')).toBeInTheDocument();
    expect(screen.queryByText('Vector')).not.toBeInTheDocument();
  });

  it('shows plain-language note for Vector when status is unknown', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk()); // vector is unknown in makeAllOk
    renderHealthDots();

    const toggle = await screen.findByTestId('health-pill-toggle');
    fireEvent.click(toggle);

    const note = screen.getByTestId('vector-optional-note');
    expect(note).toBeInTheDocument();
    expect(note).toHaveTextContent(/not running/i);
    expect(note).toHaveTextContent(/observability/i);
  });

  it('does not show vector optional note when vector status is ok', async () => {
    const summary = makeAllOk();
    // Override vector to ok
    summary.services = summary.services.map((s) =>
      s.name === 'vector' ? { ...s, status: 'ok' as const } : s,
    );
    mockFetchStackHealth.mockResolvedValue(summary);
    renderHealthDots();

    const toggle = await screen.findByTestId('health-pill-toggle');
    fireEvent.click(toggle);

    expect(screen.queryByTestId('vector-optional-note')).not.toBeInTheDocument();
  });

  // --- Compact mode ---

  it('renders compact dot row in collapsed sidebar mode', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    renderHealthDots(/* compact= */ true);

    await waitFor(() => {
      expect(screen.getByTestId('health-dots-compact')).toBeInTheDocument();
    });

    // Should not show pill toggle in compact mode
    expect(screen.queryByTestId('health-pill-toggle')).not.toBeInTheDocument();
  });

  // --- Loading state ---

  it('shows loading state while data is pending', () => {
    // fetchStackHealth never resolves in this test
    mockFetchStackHealth.mockReturnValue(new Promise(() => {}));
    renderHealthDots();

    expect(screen.getByTestId('health-dots-loading')).toBeInTheDocument();
  });

  it('settles to degraded "Status unknown" dots (not stuck "Checking…") when the probe times out', async () => {
    // fetchStackHealth applies its own hard deadline and resolves to an
    // all-unknown summary when the probes hang; simulate that resolved value.
    mockFetchStackHealth.mockResolvedValue(makeAllUnknown());
    renderHealthDots();

    // The pill must appear (i.e. we left the "Checking…" state).
    await waitFor(() => {
      expect(screen.getByTestId('health-pill-toggle')).toBeInTheDocument();
    });
    expect(screen.queryByText('Checking services…')).not.toBeInTheDocument();
    expect(screen.getByText('Status unknown')).toBeInTheDocument();

    // Expanding shows every service as 'unknown' (degraded/unknown style dots).
    fireEvent.click(screen.getByTestId('health-pill-toggle'));
    const pgRow = screen.getByTestId('health-row-postgres');
    expect(pgRow.querySelector('[aria-label*="unknown"]')).toBeInTheDocument();
  });

  it('compact mode renders unknown dots (not stuck "Checking…") when the probe times out', async () => {
    mockFetchStackHealth.mockResolvedValue(makeAllUnknown());
    renderHealthDots(/* compact= */ true);

    await waitFor(() => {
      expect(screen.getByTestId('health-dots-compact')).toBeInTheDocument();
    });
    // Loading placeholder must be gone once the (degraded) summary settles.
    expect(screen.queryByTestId('health-dots-loading')).not.toBeInTheDocument();
  });

  it('does not flash "Checking services…" during refetch when cached data exists', async () => {
    // First fetch succeeds and populates cache
    mockFetchStackHealth.mockResolvedValue(makeAllOk());
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <HealthDots />
      </QueryClientProvider>,
    );

    // Wait for initial data to render
    await screen.findByTestId('health-pill-toggle');

    // Trigger a manual refetch while mock is still pending (simulates background refresh)
    let resolveRefetch!: (v: ReturnType<typeof makeAllOk>) => void;
    mockFetchStackHealth.mockReturnValue(
      new Promise<ReturnType<typeof makeAllOk>>((res) => { resolveRefetch = res; }),
    );
    void queryClient.refetchQueries({ queryKey: QUERY_KEYS.stack.health() });

    // During the in-flight refetch the pill (cached data) must still be visible
    expect(screen.queryByText('Checking services…')).not.toBeInTheDocument();
    expect(screen.getByTestId('health-pill-toggle')).toBeInTheDocument();

    // Resolve the refetch so the test cleans up properly
    resolveRefetch(makeAllOk());
    await screen.findByTestId('health-pill-toggle');
  });

  // --- Mixed states in expanded grid ---

  it('expanded grid shows mixed statuses correctly', async () => {
    const summary: StackHealthSummary = {
      overall: 'down',
      downCount: 1,
      degradedCount: 1,
      services: [
        { name: 'paper_ingestion', label: 'Paper Ingestion', status: 'down' },
        { name: 'learning_engine', label: 'Learning Engine', status: 'degraded' },
        { name: 'postgres', label: 'PostgreSQL', status: 'ok' },
        { name: 'qdrant', label: 'Qdrant', status: 'ok' },
        { name: 'ollama', label: 'Ollama', status: 'ok' },
        { name: 'litellm', label: 'LiteLLM', status: 'ok' },
        { name: 'vector', label: 'Vector', status: 'unknown' },
      ],
    };
    mockFetchStackHealth.mockResolvedValue(summary);
    renderHealthDots();

    const toggle = await screen.findByTestId('health-pill-toggle');
    fireEvent.click(toggle);

    // The "down" row should have a red dot (aria-label contains "down")
    const piRow = screen.getByTestId('health-row-paper_ingestion');
    expect(piRow.querySelector('[aria-label*="down"]')).toBeInTheDocument();

    // The "degraded" row
    const leRow = screen.getByTestId('health-row-learning_engine');
    expect(leRow.querySelector('[aria-label*="degraded"]')).toBeInTheDocument();
  });
});

/**
 * Tests for the HealthDots component (Bucket D3).
 *
 * Covers:
 * - Collapsed pill: "All healthy" / "N degraded" / "N down" labels
 * - Click-to-expand reveals per-service grid
 * - Compact mode (sidebar-collapsed) renders dot row
 * - Loading / error states
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { HealthDots } from '@/components/shared/HealthDots';
import type { StackHealthSummary } from '@/lib/api';

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
    expect(screen.getByText('Vector')).toBeInTheDocument();
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

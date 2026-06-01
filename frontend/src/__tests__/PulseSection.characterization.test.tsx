/**
 * PulseSection — pre-decomposition behavioral snapshot.
 *
 * These tests pin the observable rendering structure of PulseSection.tsx
 * before it is extracted into sub-components.  The same tests MUST pass
 * byte-identically after extraction (data-testid attributes are preserved on
 * the extracted card wrappers).
 *
 * Scope: smoke-level (1–2 assertions each).  Exhaustive coverage lives in
 * the existing PulseSection.test.tsx suite.
 *
 * Verified identifiers:
 *   PulseSection.tsx:890  <Card data-testid="pulse-schedule-card">
 *   PulseSection.tsx:1059 <Card data-testid="pulse-weights-card">
 *   PulseSection.tsx:1303 <Card data-testid="pulse-status-card">
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PulseSection } from '@/components/settings/PulseSection';
import type { PulseStats } from '@/types';

// ---------------------------------------------------------------------------
// Module mocks — same strategy as PulseSection.test.tsx
// ---------------------------------------------------------------------------

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: '', value: null }),
    fetchPulseStats: vi.fn(),
    fetchPulseDebug: vi.fn().mockResolvedValue({
      deck_date: '2026-04-17',
      card_count: 5,
      degraded_reason: null,
      source_counts: { arxiv: 10 },
      source_diagnostics: {},
      topic_embeddings: [],
      top_cards: [],
      classifier_available: false,
      classifier_sample_count: null,
      classifier_feature_names: [],
      classifier_auc: null,
      classifier_auc_degradation_reason: null,
      classifier_degradation_reason: null,
    }),
    createJob: vi.fn().mockResolvedValue({ job_id: 'pulse-job-1', status: 'queued' }),
    listJobs: vi.fn().mockResolvedValue([]),
    getSystemCapabilities: vi.fn().mockResolvedValue({ networkx: true, scikit_learn: true }),
    patchSourceConfig: vi.fn().mockResolvedValue({ ok: true }),
    clearSourceCooldown: vi.fn().mockResolvedValue({ ok: true }),
  };
});

vi.mock('@/stores/auth-store', () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useAuthStore: vi.fn((selector: (s: any) => unknown) =>
    selector({ user: { id: 1, email: 'test@example.com', role: 'user' } }),
  ),
}));

const { fetchConfig, fetchPulseStats, getSystemCapabilities } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

const baseStats: PulseStats = {
  window_days: 1,
  decks_generated: 2,
  avg_candidates: 30,
  avg_llm_calls: 10,
  avg_duration_s: 8.5,
  last_run_at: '2026-04-10T04:00:00Z',
  last_error: null,
  degraded_reason: null,
};

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PulseSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PulseSection — pre-decomposition behavioral snapshot', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.enabled', value: false },
      { key: 'pulse.cron', value: '0 4 * * *' },
      { key: 'pulse.deck_size', value: 10 },
      { key: 'pulse.stage2_top_k', value: 40 },
    ]);
    vi.mocked(fetchPulseStats).mockResolvedValue(baseStats);
    vi.mocked(getSystemCapabilities).mockResolvedValue({ networkx: true, scikit_learn: true });
  });

  it('renders all three top-level cards under default config', async () => {
    renderSection();

    // Wait for async data fetches to settle before asserting presence
    await waitFor(() => {
      expect(screen.getByTestId('pulse-schedule-card')).toBeInTheDocument();
    });
    expect(screen.getByTestId('pulse-weights-card')).toBeInTheDocument();
    expect(screen.getByTestId('pulse-status-card')).toBeInTheDocument();
  });
});

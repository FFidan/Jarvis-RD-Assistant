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
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PulseSection } from '@/components/settings/PulseSection';
import type { PulseStats } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
import { toast } from 'sonner';

// Counted, not just observed: a failed save must produce exactly one message.
// A mutation-level handler alongside the per-call ones fires in addition to
// them, so the researcher sees the same failure reported twice.
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), message: vi.fn() },
}));

// ---------------------------------------------------------------------------
// Module mocks — same strategy as PulseSection.test.tsx
// ---------------------------------------------------------------------------

// Radix Slider replaced with a plain range input so drag/commit can be driven
// with fireEvent (same approach as IngestionSection.test.tsx).
vi.mock('@/components/ui/slider', () => ({
  Slider: ({
    id,
    min,
    max,
    step,
    value,
    onValueChange,
    onValueCommit,
    'aria-label': ariaLabel,
  }: {
    id?: string;
    min: number;
    max: number;
    step: number;
    value: number[];
    onValueChange?: (v: number[]) => void;
    onValueCommit?: (v: number[]) => void;
    'aria-label'?: string;
  }) => (
    <input
      type="range"
      id={id}
      data-testid={id}
      aria-label={ariaLabel}
      min={min}
      max={max}
      step={step}
      value={value[0]}
      onChange={(e) => onValueChange?.([Number(e.target.value)])}
      onMouseUp={(e) => onValueCommit?.([Number((e.target as HTMLInputElement).value)])}
    />
  ),
}));

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
    getSystemCapabilities: vi.fn().mockResolvedValue({ networkx: true, scikit_learn: true, structured_output_enforced: true }),
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

const { fetchConfig, fetchPulseStats, getSystemCapabilities, setConfig } =
  await import('@/lib/api');

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
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <PulseSection />
    </MemoryRouter>,
    { queryClient },
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
    vi.mocked(getSystemCapabilities).mockResolvedValue({ networkx: true, scikit_learn: true, structured_output_enforced: true });
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

  it('commits the deck size slider once, on release, not on every drag tick', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('pulse-schedule-card')).toBeInTheDocument();
    });

    const slider = screen.getByTestId('pulse-deck-size');
    fireEvent.change(slider, { target: { value: '15' } });
    fireEvent.change(slider, { target: { value: '20' } });
    expect(setConfig).not.toHaveBeenCalled();

    fireEvent.mouseUp(slider);
    await waitFor(() => {
      expect(setConfig).toHaveBeenCalledTimes(1);
    });
    expect(setConfig).toHaveBeenCalledWith('pulse.deck_size', 20);
  });

  it('gives the deck size and ranking candidate sliders a name a screen reader reads', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('pulse-schedule-card')).toBeInTheDocument();
    });

    // The name has to reach the element that reports role="slider"; a name left
    // on the surrounding row is never announced.
    expect(screen.getByRole('slider', { name: 'Deck size' })).toBe(
      screen.getByTestId('pulse-deck-size'),
    );
    expect(screen.getByRole('slider', { name: 'Ranking candidates' })).toBe(
      screen.getByTestId('pulse-stage2-top-k'),
    );
  });

  it('commits a pulse.weights signal slider once, on release, not on every drag tick', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('pulse-weights-card')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));

    const slider = await screen.findByTestId('weight-slider-embedding');
    fireEvent.change(slider, { target: { value: '0.5' } });
    fireEvent.change(slider, { target: { value: '0.6' } });
    expect(setConfig).not.toHaveBeenCalled();

    fireEvent.pointerUp(slider);
    await waitFor(() => {
      expect(setConfig).toHaveBeenCalledTimes(1);
    });
    expect(setConfig).toHaveBeenCalledWith(
      'pulse.weights',
      expect.objectContaining({ embedding: 0.6 }),
    );
  });

  it('saves a keyboard adjustment of a signal weight once, when focus leaves', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('pulse-weights-card')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));

    const slider = await screen.findByTestId('weight-slider-embedding');
    // Twenty arrow-key steps from 0 to 1. jsdom does not move a range input by
    // itself, so each step is driven the way a browser reports one: the value
    // changes, then the key comes back up.
    for (let step = 1; step <= 20; step += 1) {
      fireEvent.change(slider, { target: { value: (step * 0.05).toFixed(2) } });
      fireEvent.keyUp(slider, { key: 'ArrowRight' });
    }
    expect(setConfig).not.toHaveBeenCalled();

    fireEvent.blur(slider);
    await waitFor(() => {
      expect(setConfig).toHaveBeenCalledTimes(1);
    });
    expect(setConfig).toHaveBeenCalledWith(
      'pulse.weights',
      expect.objectContaining({ embedding: 1 }),
    );
  });

  it('reports a failed save once, naming the setting, not twice', async () => {
    // An Error with no text, so the message under test is the call site's own
    // label rather than whatever the server said.
    vi.mocked(setConfig).mockRejectedValueOnce(new Error(''));
    renderSection();
    await waitFor(() => {
      expect(screen.getByTestId('pulse-schedule-card')).toBeInTheDocument();
    });

    const slider = screen.getByTestId('pulse-deck-size');
    fireEvent.change(slider, { target: { value: '20' } });
    fireEvent.mouseUp(slider);

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledTimes(1);
    });
    expect(toast.error).toHaveBeenCalledWith('Could not update the deck size');
  });

  it('shows a stored schedule the clock picker cannot represent as read-only text', async () => {
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.enabled', value: false },
      { key: 'pulse.cron', value: '0 8,20 * * *' },
      { key: 'pulse.deck_size', value: 10 },
      { key: 'pulse.stage2_top_k', value: 40 },
    ]);
    renderSection();

    const readonlySchedule = await screen.findByTestId('pulse-cron-readonly');
    expect(readonlySchedule).toHaveTextContent('0 8,20 * * *');
  });
});

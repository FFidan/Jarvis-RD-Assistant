/**
 * PulseAdvancedTuningCard — recommendation.enabled toggle
 *
 * Verifies:
 * - Toggle renders with correct initial state (true / false)
 * - Clicking the toggle calls setMut.mutate with { key: 'recommendation.enabled', value: <negated> }
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PulseAdvancedTuningCard } from '@/components/settings/pulse/PulseAdvancedTuningCard';
import type { ConfigEntry } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchPulseDebug: vi.fn().mockResolvedValue({
      deck_date: '2026-01-01',
      card_count: 0,
      degraded_reason: null,
      source_counts: {},
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
  };
});

function makeMut(mutate = vi.fn()) {
  return {
    mutate,
    mutateAsync: vi.fn(),
    isPending: false,
    isSuccess: false,
    isError: false,
    isIdle: true,
    data: undefined,
    error: null,
    reset: vi.fn(),
    status: 'idle' as const,
    variables: undefined,
    context: undefined,
    failureCount: 0,
    failureReason: null,
    submittedAt: 0,
  } as unknown as import('@tanstack/react-query').UseMutationResult<
    unknown,
    Error,
    { key: string; value: unknown }
  >;
}

function makeConfigs(extra: ConfigEntry[] = []): ConfigEntry[] {
  return [
    { key: 'pulse.weights', value: {} },
    { key: 'recommendation.liked_weight', value: 0.6 },
    { key: 'recommendation.project_weight', value: 0.4 },
    { key: 'pulse.l2_lambda', value: 0.5 },
    ...extra,
  ];
}

function renderCard(
  props: Parameters<typeof PulseAdvancedTuningCard>[0],
) {
  const qc = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <PulseAdvancedTuningCard {...props} />
    </MemoryRouter>,
    { queryClient: qc },
  );
}

describe('PulseAdvancedTuningCard — recommendation.enabled toggle', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders toggle as checked when recommendation.enabled is true', () => {
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: true }]);
    renderCard({
      configs,
      setMut: makeMut(),
      settingsControlsDisabled: false,
      hasNetworkx: false,
      hasSklearn: false,
    });
    // Open the collapsible
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    const toggle = screen.getByTestId('recommendation-enabled-toggle');
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('renders toggle as unchecked when recommendation.enabled is false', () => {
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: false }]);
    renderCard({
      configs,
      setMut: makeMut(),
      settingsControlsDisabled: false,
      hasNetworkx: false,
      hasSklearn: false,
    });
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    const toggle = screen.getByTestId('recommendation-enabled-toggle');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
  });

  it('calls setMut.mutate with negated value when clicked', () => {
    const mutate = vi.fn();
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: true }]);
    renderCard({
      configs,
      setMut: makeMut(mutate),
      settingsControlsDisabled: false,
      hasNetworkx: false,
      hasSklearn: false,
    });
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    fireEvent.click(screen.getByTestId('recommendation-enabled-toggle'));
    // The second argument is what reports a rejected save. Asserting it here
    // stops this toggle from quietly losing the message its neighbours have.
    expect(mutate).toHaveBeenCalledWith(
      { key: 'recommendation.enabled', value: false },
      { onError: expect.any(Function) },
    );
  });

  it('does not call mutate when controls are disabled', () => {
    const mutate = vi.fn();
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: true }]);
    renderCard({
      configs,
      setMut: makeMut(mutate),
      settingsControlsDisabled: true,
      hasNetworkx: false,
      hasSklearn: false,
    });
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    fireEvent.click(screen.getByTestId('recommendation-enabled-toggle'));
    expect(mutate).not.toHaveBeenCalled();
  });

  it('states the real data prerequisites for optional signals', () => {
    renderCard({
      configs: makeConfigs(),
      setMut: makeMut(),
      settingsControlsDisabled: false,
      hasNetworkx: true,
      hasSklearn: true,
    });
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));

    expect(screen.getByText(/standard installations include their required packages/i)).toBeInTheDocument();
    expect(screen.getByText(/need citation data for the papers being ranked/i)).toBeInTheDocument();
    expect(screen.getByText(/becomes useful after about 30 pulse ratings/i)).toBeInTheDocument();
  });

  it('tells a user their ratings now affect ranking without expanding the card', () => {
    renderCard({
      configs: makeConfigs([{ key: 'pulse.classifier_opt_in', value: true }]),
      setMut: makeMut(),
      settingsControlsDisabled: false,
      hasNetworkx: true,
      hasSklearn: true,
    });

    expect(screen.getByTestId('classifier-opt-in-notice')).toBeInTheDocument();
    expect(screen.getByText(/influence how papers are ranked/i)).toBeInTheDocument();
  });

  it('turns the personal classifier off without touching the shared weights', () => {
    const mutate = vi.fn();
    renderCard({
      configs: makeConfigs([{ key: 'pulse.classifier_opt_in', value: true }]),
      setMut: makeMut(mutate),
      settingsControlsDisabled: false,
      hasNetworkx: true,
      hasSklearn: true,
    });
    fireEvent.click(screen.getByRole('button', { name: /stop using my ratings/i }));

    expect(mutate).toHaveBeenCalledWith(
      { key: 'pulse.classifier_opt_in', value: false },
      { onError: expect.any(Function) },
    );
    // pulse.weights is admin-only to write; routing the undo through it would
    // fail for exactly the users the notice is addressed to.
    expect(mutate).not.toHaveBeenCalledWith(
      expect.objectContaining({ key: 'pulse.weights' }),
      expect.anything(),
    );
  });

  it('lets a user who declined turn it back on', () => {
    const mutate = vi.fn();
    renderCard({
      configs: makeConfigs([{ key: 'pulse.classifier_opt_in', value: false }]),
      setMut: makeMut(mutate),
      settingsControlsDisabled: false,
      hasNetworkx: true,
      hasSklearn: true,
    });
    fireEvent.click(screen.getByRole('button', { name: /use my ratings/i }));

    expect(mutate).toHaveBeenCalledWith(
      { key: 'pulse.classifier_opt_in', value: true },
      { onError: expect.any(Function) },
    );
  });

  it('says nothing to a user who has not reached the threshold', () => {
    renderCard({
      configs: makeConfigs(),
      setMut: makeMut(),
      settingsControlsDisabled: false,
      hasNetworkx: true,
      hasSklearn: true,
    });

    expect(screen.queryByTestId('classifier-opt-in-notice')).not.toBeInTheDocument();
  });
});

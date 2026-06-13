/**
 * PulseAdvancedTuningCard — recommendation.enabled toggle
 *
 * Verifies:
 * - Toggle renders with correct initial state (true / false)
 * - Clicking the toggle calls setMut.mutate with { key: 'recommendation.enabled', value: <negated> }
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { PulseAdvancedTuningCard } from '@/components/settings/pulse/PulseAdvancedTuningCard';
import type { ConfigEntry } from '@/types';

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

describe('PulseAdvancedTuningCard — recommendation.enabled toggle', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders toggle as checked when recommendation.enabled is true', () => {
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: true }]);
    render(
      <PulseAdvancedTuningCard
        configs={configs}
        setMut={makeMut()}
        settingsControlsDisabled={false}
        hasNetworkx={false}
        hasSklearn={false}
      />,
    );
    // Open the collapsible
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    const toggle = screen.getByTestId('recommendation-enabled-toggle');
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('renders toggle as unchecked when recommendation.enabled is false', () => {
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: false }]);
    render(
      <PulseAdvancedTuningCard
        configs={configs}
        setMut={makeMut()}
        settingsControlsDisabled={false}
        hasNetworkx={false}
        hasSklearn={false}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    const toggle = screen.getByTestId('recommendation-enabled-toggle');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
  });

  it('calls setMut.mutate with negated value when clicked', () => {
    const mutate = vi.fn();
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: true }]);
    render(
      <PulseAdvancedTuningCard
        configs={configs}
        setMut={makeMut(mutate)}
        settingsControlsDisabled={false}
        hasNetworkx={false}
        hasSklearn={false}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    fireEvent.click(screen.getByTestId('recommendation-enabled-toggle'));
    expect(mutate).toHaveBeenCalledWith({ key: 'recommendation.enabled', value: false });
  });

  it('does not call mutate when controls are disabled', () => {
    const mutate = vi.fn();
    const configs = makeConfigs([{ key: 'recommendation.enabled', value: true }]);
    render(
      <PulseAdvancedTuningCard
        configs={configs}
        setMut={makeMut(mutate)}
        settingsControlsDisabled={true}
        hasNetworkx={false}
        hasSklearn={false}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /advanced tuning/i }));
    fireEvent.click(screen.getByTestId('recommendation-enabled-toggle'));
    expect(mutate).not.toHaveBeenCalled();
  });
});

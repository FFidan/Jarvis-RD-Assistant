import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AutomationStep } from '@/pages/onboarding/AutomationStep';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({}),
  };
});

const { fetchConfig, setConfig } = await import('@/lib/api');

function renderStep() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <AutomationStep stepNumber={5} totalSteps={8} onBack={vi.fn()} onNext={vi.fn()} />,
    { queryClient },
  );
}

describe('AutomationStep', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('saves a plain daily cron when no schedule is stored yet', async () => {
    vi.mocked(fetchConfig).mockResolvedValue([]);
    const user = userEvent.setup();
    renderStep();

    await screen.findByText('Daily run time');
    await user.click(screen.getByRole('button', { name: /save schedule/i }));

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('pulse.cron', '0 4 * * *');
    });
  });

  it('keeps a stored schedule the clock picker cannot represent instead of overwriting it', async () => {
    // Hourly — the minute/hour fields carry a wildcard, so isTimeOnlyCron is false
    // and a single HH:MM picker cannot show it (see cron-utils.test.ts).
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.cron', value: '0 * * * *' },
      { key: 'pulse.enabled', value: true },
    ]);
    const user = userEvent.setup();
    renderStep();

    expect(await screen.findByText(/Pulse already has a schedule set: Every hour/)).toBeInTheDocument();
    expect(screen.queryByText('Daily run time')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /save schedule/i }));

    // Saving must not collapse the hourly schedule down to a plain daily time.
    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('pulse.cron', '0 * * * *');
    });
  });

  it('shows an unparseable stored schedule as a labelled value, not embedded prose', async () => {
    // cronToHumanReadable has no short honest phrase for an expression it
    // cannot parse and falls back to the raw string — the sentence must
    // still read as a displayed value rather than a broken English clause.
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.cron', value: 'not-a-cron' },
      { key: 'pulse.enabled', value: true },
    ]);
    renderStep();

    expect(
      await screen.findByText('Pulse already has a schedule set: not-a-cron.', { exact: false }),
    ).toBeInTheDocument();
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AutomationSection } from '@/components/settings/AutomationSection';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// Mock Radix Select with native HTML elements (portals do not work in jsdom).
// Capture onValueChange so tests can invoke it directly.
let _selectOnValueChange: ((v: string) => void) | undefined;

vi.mock('@/components/ui/select', () => ({
  Select: ({ children, onValueChange }: any) => {
    _selectOnValueChange = onValueChange;
    return <div>{children}</div>;
  },
  SelectTrigger: ({ children }: any) => <button data-testid="select-trigger">{children}</button>,
  SelectValue: ({ placeholder }: any) => <span>{placeholder}</span>,
  SelectContent: ({ children }: any) => <div>{children}</div>,
  SelectGroup: ({ children }: any) => <div>{children}</div>,
  SelectLabel: ({ children }: any) => <div>{children}</div>,
  SelectSeparator: () => <hr />,
  SelectItem: ({ children, value }: any) => (
    <div
      role="option"
      onClick={() => _selectOnValueChange?.(value)}
    >
      {children}
    </div>
  ),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchNudges: vi.fn(),
    fetchConfig: vi.fn().mockResolvedValue([]),
    updateNudge: vi.fn().mockResolvedValue({}),
    setConfig: vi.fn().mockResolvedValue({ key: 'automation.fetch_interval_hours', value: 6 }),
  };
});

const { fetchNudges, fetchConfig, setConfig } = await import('@/lib/api');

function renderSection() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <AutomationSection />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('AutomationSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNudges).mockResolvedValue([]);
    vi.mocked(fetchConfig).mockResolvedValue([]);
    _selectOnValueChange = undefined;
  });

  it('shows empty state when no nudges are configured', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/No automation jobs/i)).toBeInTheDocument();
    });
    expect(screen.queryByText('Failed to load automation jobs.')).toBeNull();
  });

  it('shows an error message, not the empty state, when nudges fail to load', async () => {
    vi.mocked(fetchNudges).mockRejectedValue(new Error('network down'));
    renderSection();
    expect(await screen.findByText('Failed to load automation jobs.')).toBeInTheDocument();
    expect(screen.queryByText(/No automation jobs/i)).toBeNull();
  });

  it('shows an error message when settings values fail to load', async () => {
    vi.mocked(fetchConfig).mockRejectedValue(new Error('network down'));
    renderSection();
    expect(await screen.findByText('Failed to load automation settings.')).toBeInTheDocument();
  });

  it('keeps config controls disabled and never persists fallbacks when settings fail to load', async () => {
    vi.mocked(fetchConfig).mockRejectedValue(new Error('network down'));
    vi.mocked(fetchNudges).mockResolvedValue([
      {
        id: 5,
        nudge_type: 'daily_summary',
        enabled: true,
        cron_expression: '0 8 * * *',
        last_fired_at: null,
        config: {},
        created_at: '2026-04-17T00:00:00Z',
      },
    ]);
    const user = userEvent.setup();
    const { unmount } = renderSection();
    expect(await screen.findByText('Failed to load automation settings.')).toBeInTheDocument();

    // Timezone combobox must be disabled and must not open.
    const tzTrigger = await screen.findByRole('combobox');
    expect(tzTrigger).toBeDisabled();
    await user.click(tzTrigger);
    expect(screen.queryByPlaceholderText('Search city or timezone...')).toBeNull();

    // Fetch-interval input must be disabled, and even a programmatic
    // change-plus-blur must not write the fabricated default to the server.
    const intervalInput = screen.getByRole('spinbutton');
    expect(intervalInput).toBeDisabled();
    fireEvent.change(intervalInput, { target: { value: '6' } });
    fireEvent.blur(intervalInput);
    expect(vi.mocked(setConfig)).not.toHaveBeenCalled();

    // This case deliberately leaves a query in its error state, which queues a
    // batched TanStack Query notification on a timer. Drop the subscriber here
    // rather than at teardown, so the notification has nothing to deliver.
    unmount();
  });

  it('renders nudge card when nudges are returned', async () => {
    vi.mocked(fetchNudges).mockResolvedValue([
      {
        id: 1,
        nudge_type: 'review_reminder',
        enabled: true,
        cron_expression: '0 8 * * *',
        last_fired_at: null,
        config: {},
        created_at: '2026-04-17T00:00:00Z',
      },
    ]);
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('Flashcard Review Reminder')).toBeInTheDocument();
    });
  });

  it('switches nudge enabled state on button click', async () => {
    const { updateNudge } = await import('@/lib/api');
    vi.mocked(fetchNudges).mockResolvedValue([
      {
        id: 2,
        nudge_type: 'daily_summary',
        enabled: false,
        cron_expression: '0 7 * * *',
        last_fired_at: null,
        config: {},
        created_at: '2026-04-17T00:00:00Z',
      },
    ]);
    const user = userEvent.setup();
    renderSection();
    const enableBtn = await screen.findByRole('button', { name: /enable/i });
    await user.click(enableBtn);
    await waitFor(() => {
      expect(vi.mocked(updateNudge)).toHaveBeenCalledWith(2, { enabled: true });
    });
  });

  it('cancels in-flight debounce timeout when NudgeRow unmounts', async () => {
    vi.mocked(fetchNudges).mockResolvedValue([
      {
        id: 4,
        nudge_type: 'review_reminder',
        enabled: true,
        cron_expression: '0 8 * * *',
        last_fired_at: null,
        config: {},
        created_at: '2026-04-17T00:00:00Z',
      },
    ]);

    const { unmount } = renderSection();
    await waitFor(() => {
      expect(screen.getByText('Flashcard Review Reminder')).toBeInTheDocument();
    });

    // Spy on setTimeout to capture the debounce timer ID, and on clearTimeout to
    // assert the exact ID is cancelled when the component unmounts.
    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout');
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');

    // Arm the debounce inside NudgeRow by triggering onValueChange on the TimeSelect's
    // minutes Select (the last Select rendered by TimeSelect; our Radix mock overwrites
    // _selectOnValueChange on each Select, so it always holds the final one — minutes).
    act(() => {
      _selectOnValueChange?.('15');
    });

    // Verify a setTimeout was registered by handleTimeChange
    expect(setTimeoutSpy).toHaveBeenCalled();
    // The last setTimeout call is the debounce (300ms)
    const debounceTimerId = setTimeoutSpy.mock.results[setTimeoutSpy.mock.results.length - 1]?.value;
    expect(debounceTimerId).toBeDefined();

    // Unmount BEFORE the 300ms debounce elapses — the cleanup useEffect must call
    // clearTimeout with the exact debounce timer ID.
    unmount();

    expect(clearTimeoutSpy).toHaveBeenCalledWith(debounceTimerId);

    setTimeoutSpy.mockRestore();
    clearTimeoutSpy.mockRestore();
  });

  it('renders auto-fetch interval input and fires setConfig mutation on blur', async () => {
    // Seed config with a known value for automation.fetch_interval_hours
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'automation.fetch_interval_hours', value: 24 },
    ]);
    vi.mocked(fetchNudges).mockResolvedValue([
      {
        id: 3,
        nudge_type: 'daily_summary',
        enabled: true,
        cron_expression: '0 8 * * *',
        last_fired_at: null,
        config: {},
        created_at: '2026-04-17T00:00:00Z',
      },
    ]);

    renderSection();

    // The numeric input must be present; its value arrives once config loads
    const input = await screen.findByRole('spinbutton');
    expect(input).toBeInTheDocument();
    await waitFor(() => {
      expect((input as HTMLInputElement).value).toBe('24');
    });

    // Change value and blur to trigger mutation
    fireEvent.change(input, { target: { value: '6' } });
    fireEvent.blur(input);

    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith(
        'automation.fetch_interval_hours',
        6,
      );
    });
  });
});

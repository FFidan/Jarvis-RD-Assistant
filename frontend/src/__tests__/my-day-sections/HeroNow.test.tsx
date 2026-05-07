import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HeroNow } from '@/components/my-day/sections/HeroNow';
import { useUIStore } from '@/stores/ui-store';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const startWorkMock = vi.fn();

// Mutable state so individual tests can override hasTask
let pomodoroState = { phase: 'idle' as string, attachedItem: null as { id: number; title: string; type: string } | null, pausedAt: null as number | null, secondsRemaining: 0 };

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (state: typeof pomodoroState) => unknown) => {
      return selector ? selector(pomodoroState) : pomodoroState;
    },
    { getState: () => ({ startWork: startWorkMock }) },
  ),
}));

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      isRunning: () => false,
      startJob: vi.fn(),
      trackExternalJob: vi.fn(),
    }),
  ),
}));

vi.mock('@/lib/api', () => ({
  fetchPulseToday: vi.fn(),
  ratePulseCard: vi.fn(),
  fetchMyDay: vi.fn(),
  getStats: vi.fn(),
}));

const { fetchPulseToday } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HeroNow />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('HeroNow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Reset Zustand ui-store to default state (W4-23: heroMode moved from localStorage to ui-store)
    useUIStore.setState({ heroMode: 'pulse' });
    // Reset to idle (no active task)
    pomodoroState = { phase: 'idle', attachedItem: null, pausedAt: null, secondsRemaining: 0 };
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
  });

  it('defaults to Pulse mode on first render', async () => {
    renderWithProviders();

    // Wait for HeroPulse to render
    expect(await screen.findByText(/No Pulse for today yet/i)).toBeInTheDocument();

    // ModePicker buttons have role="tab" (W2-18 ARIA enhancement)
    const pulseBtn = screen.getByRole('tab', { name: 'Pulse #1' });
    expect(pulseBtn).toBeInTheDocument();
  });

  it('Continue task tab is hidden when no Pomodoro is active', async () => {
    renderWithProviders();
    await screen.findByText(/No Pulse for today yet/i);

    // With phase=idle, Continue task tab should not exist
    expect(screen.queryByRole('tab', { name: 'Continue task' })).not.toBeInTheDocument();
  });

  it('Continue task tab appears when Pomodoro is active', async () => {
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 1, title: 'Test task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1500,
    };

    renderWithProviders();
    await screen.findByText(/No Pulse for today yet/i);

    expect(screen.getByRole('tab', { name: 'Continue task' })).toBeInTheDocument();
  });

  it('clicking Continue task switches mode and persists to Zustand ui-store', async () => {
    const user = userEvent.setup();
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 1, title: 'Test task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1500,
    };

    renderWithProviders();
    await screen.findByText(/No Pulse for today yet/i);

    const taskBtn = screen.getByRole('tab', { name: 'Continue task' });
    await user.click(taskBtn);

    // heroMode is now persisted via Zustand ui-store (jarvis-ui key), not raw localStorage
    const { useUIStore } = await import('@/stores/ui-store');
    expect(useUIStore.getState().heroMode).toBe('task');
  });

  it('Resume reading tab is gone (no thread data in Phase 1c)', async () => {
    renderWithProviders();
    await screen.findByText(/No Pulse for today yet/i);

    // Resume reading tab was removed per SPEC §States (no backend data)
    expect(screen.queryByText(/Resume reading/i)).not.toBeInTheDocument();
  });
});

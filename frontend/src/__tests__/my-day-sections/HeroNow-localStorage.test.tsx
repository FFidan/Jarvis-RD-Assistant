/**
 * HeroNow — Zustand ui-store persistence tests.
 * W4-23: heroMode moved from raw localStorage ('myday.heroMode') to
 * Zustand persist store (UI_STORE_KEY = 'jarvis-ui'). These tests verify
 * the Zustand-based persistence behavior.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HeroNow } from '@/components/my-day/sections/HeroNow';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const startWorkMock = vi.fn();

let pomodoroState = {
  phase: 'work' as string,
  attachedItem: { id: 1, title: 'Active task', type: 'task' } as { id: number; title: string; type: string } | null,
  pausedAt: null as number | null,
  secondsRemaining: 1500,
};

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
const { useUIStore } = await import('@/stores/ui-store');

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

describe('HeroNow — Zustand ui-store persistence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Reset Zustand ui-store to default state
    useUIStore.setState({ heroMode: 'pulse' });
    // Default: Pomodoro active so both mode buttons are visible
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 1, title: 'Active task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1500,
    };
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
  });

  it('hydrates from ui-store "task" heroMode and shows Continue task as active mode', async () => {
    // Pre-set heroMode in Zustand store before render
    useUIStore.setState({ heroMode: 'task' });

    renderWithProviders();

    // Continue task tab should be present (active Pomodoro)
    const taskBtn = await screen.findByRole('tab', { name: 'Continue task' });
    expect(taskBtn).toBeInTheDocument();
    // Pulse #1 tab should also be present
    expect(screen.getByRole('tab', { name: 'Pulse #1' })).toBeInTheDocument();
  });

  it('switching from pulse to task updates ui-store heroMode to "task"', async () => {
    const user = userEvent.setup();
    useUIStore.setState({ heroMode: 'pulse' });

    renderWithProviders();

    // Wait for render
    await screen.findByText(/No Pulse for today yet/i);

    // Both tabs present (active Pomodoro)
    const taskBtn = screen.getByRole('tab', { name: 'Continue task' });
    await user.click(taskBtn);

    expect(useUIStore.getState().heroMode).toBe('task');
  });

  it('defaults to "pulse" when ui-store heroMode is at its default', async () => {
    // Default state — heroMode = 'pulse'
    useUIStore.setState({ heroMode: 'pulse' });

    renderWithProviders();

    // Pulse content renders (default pulse mode)
    expect(await screen.findByText(/No Pulse for today yet/i)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Pulse #1' })).toBeInTheDocument();
  });
});

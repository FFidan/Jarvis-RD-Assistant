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

describe('HeroNow — localStorage persistence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Default: Pomodoro active so both mode buttons are visible
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 1, title: 'Active task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1500,
    };
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
  });

  it('hydrates from localStorage "task" and shows Continue task as active mode', async () => {
    // Pre-set before render so useEffect picks it up
    localStorage.setItem('myday.heroMode', 'task');

    renderWithProviders();

    // Continue task button should be present (active Pomodoro)
    const taskBtn = await screen.findByRole('button', { name: 'Continue task' });
    expect(taskBtn).toBeInTheDocument();
    // Pulse #1 button should also be present
    expect(screen.getByRole('button', { name: 'Pulse #1' })).toBeInTheDocument();
  });

  it('switching from pulse to task updates localStorage to "task"', async () => {
    const user = userEvent.setup();
    localStorage.setItem('myday.heroMode', 'pulse');

    renderWithProviders();

    // Wait for render
    await screen.findByText(/No Pulse for today yet/i);

    // Both buttons present (active Pomodoro)
    const taskBtn = screen.getByRole('button', { name: 'Continue task' });
    await user.click(taskBtn);

    expect(localStorage.getItem('myday.heroMode')).toBe('task');
  });

  it('falls back to "pulse" when localStorage has stale "resume" value', async () => {
    // "resume" was the old Phase 1b tab value — should fall back to "pulse"
    localStorage.setItem('myday.heroMode', 'resume');

    renderWithProviders();

    // Pulse content renders (fallback to pulse mode), not a crash or empty state
    expect(await screen.findByText(/No Pulse for today yet/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pulse #1' })).toBeInTheDocument();
  });
});

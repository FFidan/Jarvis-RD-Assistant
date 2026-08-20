/**
 * HeroNow — localStorage('myday.heroMode') persistence.
 * The v3 design persists the user's hero focus choice to a raw
 * localStorage key (matching the runnable prototype + the e2e walk),
 * independent of the shared ui-store.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { HeroNow } from '@/components/my-day/sections/HeroNow';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const startWorkMock = vi.fn();

let pomodoroState = {
  phase: 'work' as string,
  attachedItem: { id: 1, title: 'Active task', type: 'task' } as
    | { id: number; title: string; type: string }
    | null,
  pausedAt: null as number | null,
  secondsRemaining: 1500,
  cyclesCompleted: 0,
  targetCycles: 4,
};

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (state: typeof pomodoroState) => unknown) => {
      return selector ? selector(pomodoroState) : pomodoroState;
    },
    { getState: () => ({ startWork: startWorkMock, resume: vi.fn(), pause: vi.fn() }) },
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
  fetchConfig: vi.fn(),
  fetchPulseToday: vi.fn(),
  ratePulseCard: vi.fn(),
  fetchMyDay: vi.fn(),
  getStats: vi.fn(),
  fetchFeed: vi.fn(),
  fetchThreads: vi.fn(),
  resumeThread: vi.fn(),
  updateTask: vi.fn(),
  logFocusSession: vi.fn().mockResolvedValue({ status: 'ok', recorded_hours: 0 }),
}));

const { fetchConfig, fetchPulseToday, fetchFeed, fetchThreads } = await import('@/lib/api');

function renderSubject() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <HeroNow />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('HeroNow — localStorage persistence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 1, title: 'Active task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1500,
      cyclesCompleted: 0,
      targetCycles: 4,
    };
    vi.mocked(fetchConfig).mockResolvedValue([]);
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
    vi.mocked(fetchFeed).mockResolvedValue({ papers: [], total: 0 } as never);
    vi.mocked(fetchThreads).mockResolvedValue([]);
  });

  it('hydrates from a stored "task" heroMode and shows the task hero', async () => {
    localStorage.setItem('myday.heroMode', 'task');
    renderSubject();
    // HeroTask renders the active item title
    expect(await screen.findByText('Active task')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Continue task' })).toBeInTheDocument();
  });

  it('switching from pulse to task writes "task" to localStorage', async () => {
    const user = userEvent.setup();
    // Explicit stored "pulse" choice wins over the smart default.
    localStorage.setItem('myday.heroMode', 'pulse');
    renderSubject();
    await screen.findByText(/No Pulse for today yet/i);

    const taskBtn = screen.getByRole('tab', { name: 'Continue task' });
    await user.click(taskBtn);

    expect(localStorage.getItem('myday.heroMode')).toBe('task');
  });

  it('defaults to pulse content when there is no stored choice and no active focus', async () => {
    pomodoroState = {
      phase: 'idle',
      attachedItem: null,
      pausedAt: null,
      secondsRemaining: 0,
      cyclesCompleted: 0,
      targetCycles: 4,
    };
    renderSubject();
    expect(await screen.findByText(/No Pulse for today yet/i)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Pulse #1' })).toBeInTheDocument();
  });
});

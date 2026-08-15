/**
 * HeroNow — 3-mode picker + smart default.
 * heroMode is persisted to localStorage('myday.heroMode')
 * (not the shared ui-store), matching the runnable prototype.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { HeroNow } from '@/components/my-day/sections/HeroNow';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const startWorkMock = vi.fn();

let pomodoroState = {
  phase: 'idle' as string,
  attachedItem: null as { id: number; title: string; type: string } | null,
  pausedAt: null as number | null,
  secondsRemaining: 0,
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

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderSubject() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <HeroNow />
    </MemoryRouter>,
    { queryClient },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('HeroNow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    pomodoroState = {
      phase: 'idle',
      attachedItem: null,
      pausedAt: null,
      secondsRemaining: 0,
      cyclesCompleted: 0,
      targetCycles: 4,
    };
    vi.mocked(fetchConfig).mockResolvedValue([]);
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
    vi.mocked(fetchFeed).mockResolvedValue({ papers: [], total: 0 } as never);
    vi.mocked(fetchThreads).mockResolvedValue([]);
  });

  it('defaults to Pulse mode on first render', async () => {
    renderSubject();
    expect(await screen.findByText(/No Pulse for today yet/i)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Pulse #1' })).toBeInTheDocument();
  });

  it('Continue task tab is hidden when no Pomodoro is active', async () => {
    renderSubject();
    await screen.findByText(/No Pulse for today yet/i);
    expect(screen.queryByRole('tab', { name: 'Continue task' })).not.toBeInTheDocument();
  });

  it('Continue task tab appears when Pomodoro is active', async () => {
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 1, title: 'Test task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1500,
      cyclesCompleted: 0,
      targetCycles: 4,
    };
    renderSubject();
    // Smart default selects task mode (active Pomodoro, no stored choice).
    expect(await screen.findByRole('tab', { name: 'Continue task' })).toBeInTheDocument();
    expect(screen.getByText('Test task')).toBeInTheDocument();
  });

  it('clicking Continue task persists to localStorage("myday.heroMode")', async () => {
    const user = userEvent.setup();
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 1, title: 'Test task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1500,
      cyclesCompleted: 0,
      targetCycles: 4,
    };
    renderSubject();
    await screen.findByText(/Test task/i);

    const taskBtn = screen.getByRole('tab', { name: 'Continue task' });
    await user.click(taskBtn);

    expect(localStorage.getItem('myday.heroMode')).toBe('task');
  });

  it('Resume thread tab appears when an open thread exists', async () => {
    vi.mocked(fetchThreads).mockResolvedValue([
      {
        id: 9,
        title: 'Memory bound proof',
        anchor: 'notebook §4.2',
        progress: 0.85,
        last_at: '2026-05-15T09:00:00Z',
        status: 'open',
        created_at: '2026-05-10T00:00:00Z',
      },
    ]);
    renderSubject();
    expect(await screen.findByRole('tab', { name: 'Resume thread' })).toBeInTheDocument();
  });

  it('smart default selects task mode when a Pomodoro is active and no stored choice', async () => {
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 2, title: 'Active focus task', type: 'task' },
      pausedAt: null,
      secondsRemaining: 1200,
      cyclesCompleted: 0,
      targetCycles: 4,
    };
    renderSubject();
    // HeroTask renders the attached item title
    expect(await screen.findByText('Active focus task')).toBeInTheDocument();
  });
});

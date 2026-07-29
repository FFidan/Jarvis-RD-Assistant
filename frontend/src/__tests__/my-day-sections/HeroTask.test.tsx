/**
 * HeroTask — Pomodoro controls: Pause/Resume, Skip break,
 * Stop & log, and cycle progress dots.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { HeroTask } from '@/components/my-day/sections/HeroTask';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const stopAndLogMock = vi.fn();
const skipBreakMock = vi.fn();
const pauseMock = vi.fn();
const resumeMock = vi.fn();

let pomodoroState = {
  phase: 'work' as string,
  attachedItem: { id: 7, title: 'Vector-field capacity argument', type: 'task' } as
    | { id: number; title: string; type: string }
    | null,
  pausedAt: null as number | null,
  secondsRemaining: 900,
  cyclesCompleted: 1,
  targetCycles: 4,
};

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (s: typeof pomodoroState) => unknown) =>
      selector ? selector(pomodoroState) : pomodoroState,
    {
      getState: () => ({
        stopAndLog: stopAndLogMock,
        skipBreak: skipBreakMock,
        pause: pauseMock,
        resume: resumeMock,
      }),
    },
  ),
}));

vi.mock('@/lib/api', () => ({
  updateTask: vi.fn(),
  logFocusSession: vi.fn().mockResolvedValue({ status: 'ok', recorded_hours: 0.25 }),
}));

function renderHero() {
  const qc = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <HeroTask />
    </MemoryRouter>,
    { queryClient: qc },
  );
}

describe('HeroTask Pomodoro controls', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    pomodoroState = {
      phase: 'work',
      attachedItem: { id: 7, title: 'Vector-field capacity argument', type: 'task' },
      pausedAt: null,
      secondsRemaining: 900,
      cyclesCompleted: 1,
      targetCycles: 4,
    };
  });

  it('renders cycle dots — one per target cycle, completed ones marked done', () => {
    renderHero();
    const dots = screen.getAllByTestId('cycle-dot');
    expect(dots).toHaveLength(4);
    expect(dots.filter((d) => d.getAttribute('data-state') === 'done')).toHaveLength(1);
    expect(dots.filter((d) => d.getAttribute('data-state') === 'current')).toHaveLength(1);
  });

  it('shows Pause during a running work phase and calls store.pause', async () => {
    const user = userEvent.setup();
    renderHero();
    const pauseBtn = screen.getByRole('button', { name: /^pause$/i });
    await user.click(pauseBtn);
    expect(pauseMock).toHaveBeenCalledOnce();
  });

  it('shows Resume when paused and calls store.resume', async () => {
    const user = userEvent.setup();
    pomodoroState = { ...pomodoroState, pausedAt: Date.now() };
    renderHero();
    const resumeBtn = screen.getByRole('button', { name: /resume/i });
    await user.click(resumeBtn);
    expect(resumeMock).toHaveBeenCalledOnce();
  });

  it('shows Skip break during a break phase and calls store.skipBreak', async () => {
    const user = userEvent.setup();
    pomodoroState = { ...pomodoroState, phase: 'short-break' };
    renderHero();
    const skipBtn = screen.getByRole('button', { name: /skip break/i });
    await user.click(skipBtn);
    expect(skipBreakMock).toHaveBeenCalledOnce();
  });

  it('Stop & log calls stopAndLog and logs the elapsed focus session', async () => {
    const user = userEvent.setup();
    stopAndLogMock.mockReturnValue({ durationSeconds: 600, taskId: 7 });
    const { logFocusSession } = await import('@/lib/api');
    renderHero();

    const stopBtn = screen.getByRole('button', { name: /stop & log/i });
    await user.click(stopBtn);

    expect(stopAndLogMock).toHaveBeenCalledOnce();
    expect(vi.mocked(logFocusSession).mock.calls[0]?.[0]).toEqual({
      duration_hours: 600 / 3600,
      task_id: 7,
      paper_id: undefined,
    });
  });

  it('shows the idle placeholder when no Pomodoro is active', () => {
    pomodoroState = {
      phase: 'idle',
      attachedItem: null,
      pausedAt: null,
      secondsRemaining: 0,
      cyclesCompleted: 0,
      targetCycles: 4,
    };
    renderHero();
    expect(screen.getByText(/No active task/i)).toBeInTheDocument();
  });
});

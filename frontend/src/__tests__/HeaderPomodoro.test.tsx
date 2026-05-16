/**
 * HeaderPomodoro — §1c stop/dismiss affordance plus the existing
 * pause/resume toggle. The stop control lets a Pomodoro be ended off
 * the My-Day page.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HeaderPomodoro } from '@/components/layout/HeaderPomodoro';

const pauseMock = vi.fn();
const resumeMock = vi.fn();
const stopAndLogMock = vi.fn();

let pomodoroState = {
  phase: 'work' as string,
  secondsRemaining: 1500,
  pausedAt: null as number | null,
  attachedItem: { id: 1, title: 'Thesis §3.2', type: 'task' } as
    | { id: number; title: string; type: string }
    | null,
  pause: pauseMock,
  resume: resumeMock,
};

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (s: typeof pomodoroState) => unknown) =>
      selector ? selector(pomodoroState) : pomodoroState,
    {
      getState: () => ({
        pause: pauseMock,
        resume: resumeMock,
        stopAndLog: stopAndLogMock,
      }),
    },
  ),
}));

vi.mock('@/lib/api', () => ({
  logFocusSession: vi.fn().mockResolvedValue({ status: 'ok', recorded_hours: 0.5 }),
}));

function renderHeader() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HeaderPomodoro />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('HeaderPomodoro', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    pomodoroState = {
      phase: 'work',
      secondsRemaining: 1500,
      pausedAt: null,
      attachedItem: { id: 1, title: 'Thesis §3.2', type: 'task' },
      pause: pauseMock,
      resume: resumeMock,
    };
  });

  it('renders nothing when idle', () => {
    pomodoroState = {
      phase: 'idle',
      secondsRemaining: 0,
      pausedAt: null,
      attachedItem: null,
      pause: pauseMock,
      resume: resumeMock,
    };
    const { container } = renderHeader();
    expect(container).toBeEmptyDOMElement();
  });

  it('keeps the pause/resume toggle (back-compat with layout spec)', async () => {
    const user = userEvent.setup();
    renderHeader();
    const pauseBtn = screen.getByRole('button', { name: /pause pomodoro/i });
    await user.click(pauseBtn);
    expect(pauseMock).toHaveBeenCalledOnce();
  });

  it('exposes a Stop affordance that ends and logs the session', async () => {
    const user = userEvent.setup();
    stopAndLogMock.mockReturnValue({ durationSeconds: 1800, taskId: 1 });
    const { logFocusSession } = await import('@/lib/api');
    renderHeader();

    const stopBtn = screen.getByRole('button', { name: /stop pomodoro/i });
    await user.click(stopBtn);

    expect(stopAndLogMock).toHaveBeenCalledOnce();
    expect(vi.mocked(logFocusSession).mock.calls[0]?.[0]).toEqual({
      duration_hours: 0.5,
      task_id: 1,
      paper_id: undefined,
    });
  });
});

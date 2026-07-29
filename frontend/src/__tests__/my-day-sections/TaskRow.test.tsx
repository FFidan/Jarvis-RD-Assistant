import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { TaskRow } from '@/components/my-day/sections/TaskRow';
import type { MyDayTask } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const startWorkMock = vi.fn();

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (state: any) => any) => {
      const state = { phase: 'idle', attachedItem: null };
      return selector ? selector(state) : state;
    },
    { getState: () => ({ startWork: startWorkMock }) },
  ),
}));

vi.mock('@/lib/api', () => ({
  updateTask: vi.fn(),
  deleteTask: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const { updateTask, deleteTask } = await import('@/lib/api');
const { toast } = await import('sonner');

// ---------------------------------------------------------------------------
// Shared fixture
// ---------------------------------------------------------------------------

const MOCK_TASK: MyDayTask = {
  id: 42,
  project_id: null,
  title: 'Write unit tests',
  priority: 1,
  deadline: null,
  status: 'todo',
  completed_at: null,
  project_name: null,
  project_color: null,
};

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderRow(task: MyDayTask = MOCK_TASK, isTimerActive = false, index = 0) {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <TaskRow task={task} index={index} isTimerActive={isTimerActive} />
    </MemoryRouter>,
    { queryClient },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('TaskRow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(updateTask).mockResolvedValue(undefined as any);
    vi.mocked(deleteTask).mockResolvedValue(undefined as any);
  });

  it('clicking ▶ Focus button calls pomodoroStore.startWork with correct args', async () => {
    const user = userEvent.setup();
    renderRow();

    // The focus button is in the DOM but visually hidden (opacity-0); it is still interactive
    const focusBtn = screen.getByTitle(/Start 25:00 Pomodoro/i);
    await user.click(focusBtn);

    expect(startWorkMock).toHaveBeenCalledOnce();
    expect(startWorkMock).toHaveBeenCalledWith({
      id: MOCK_TASK.id,
      title: MOCK_TASK.title,
      type: 'task',
    });
  });

  it('clicking completion circle button calls updateTask with status done', async () => {
    const user = userEvent.setup();
    renderRow();

    const completeBtn = screen.getByRole('button', { name: /Mark task done/i });
    await user.click(completeBtn);

    expect(updateTask).toHaveBeenCalledOnce();
    expect(updateTask).toHaveBeenCalledWith(MOCK_TASK.id, { status: 'done' });
  });

  it('▶ Focus button is disabled when isTimerActive=true', () => {
    renderRow(MOCK_TASK, true);

    const focusBtn = screen.getByTitle(/A Pomodoro is already running/i);
    expect(focusBtn).toBeDisabled();
  });

  it('shows error toast when complete mutation fails', async () => {
    const user = userEvent.setup();
    vi.mocked(updateTask).mockRejectedValue(new Error('Network error'));
    renderRow();

    const completeBtn = screen.getByRole('button', { name: /Mark task done/i });
    await user.click(completeBtn);

    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Failed to mark done: Network error',
    );
  });

  it('shows error toast when delete mutation fails', async () => {
    const user = userEvent.setup();
    vi.mocked(deleteTask).mockRejectedValue(new Error('Server error'));
    renderRow();

    const deleteBtn = screen.getByRole('button', { name: /Delete task/i });
    await user.click(deleteBtn);

    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Failed to delete task: Server error',
    );
  });

  it('the "Mark task done" control carries data-touch-target (44px tap uplift)', () => {
    renderRow();
    expect(screen.getByRole('button', { name: /Mark task done/i })).toHaveAttribute('data-touch-target');
  });

  it('the delete button becomes visible on keyboard focus', () => {
    renderRow();
    expect(screen.getByRole('button', { name: /Delete task/i })).toHaveClass('focus-visible:opacity-100');
  });
});

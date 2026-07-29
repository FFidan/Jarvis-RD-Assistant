import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QUERY_KEYS } from '@/lib/query-keys';
import { MemoryRouter } from 'react-router-dom';
import { IntentSection } from '@/components/my-day/sections/IntentSection';
import type { MyDayResponse } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: (selector?: (state: { phase: string }) => unknown) => {
    const state = { phase: 'idle' };
    return selector ? selector(state) : state;
  },
}));

vi.mock('@/lib/api', () => ({
  fetchMyDay: vi.fn(),
  createQuickTask: vi.fn(),
  updateTask: vi.fn(),
  fetchIntentToday: vi.fn().mockResolvedValue({ intent: null, updated_at: null }),
  saveIntentToday: vi.fn().mockResolvedValue({ intent: null, updated_at: null }),
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

const { fetchMyDay, updateTask } = await import('@/lib/api');
const { toast } = await import('sonner');

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const BASE_MY_DAY: MyDayResponse = {
  tasks: [],
  cards_due: 0,
  recommendations: [],
  today_focus_hours: 0,
  focus_streak_days: 0,
  project_pulse: [],
};

const COMPLETED_TASK = {
  id: 99,
  project_id: null,
  title: 'Write the weekly report',
  priority: 2,
  deadline: null,
  status: 'done' as const,
  completed_at: '2026-05-03T10:00:00Z',
  project_name: null,
  project_color: null,
};

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderSubject() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <IntentSection />
    </MemoryRouter>,
    { queryClient },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('IntentSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(updateTask).mockResolvedValue(undefined as any);
  });

  describe('ChevronRight toggle for completed tasks', () => {
    it('renders "N done today" toggle button when completed tasks exist', async () => {
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [COMPLETED_TASK],
      });

      renderSubject();

      // The toggle shows "1 done today" when showCompleted is false (ChevronRight state)
      expect(await screen.findByText(/1 done today/)).toBeInTheDocument();
    });

    it('does not render completed-tasks toggle when no tasks are done', async () => {
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [
          {
            id: 1,
            project_id: null,
            title: 'Pending task',
            priority: 3,
            deadline: null,
            status: 'todo' as const,
            completed_at: null,
            project_name: null,
            project_color: null,
          },
        ],
      });

      renderSubject();

      // Wait for data to load
      expect(await screen.findByText('Pending task')).toBeInTheDocument();
      expect(screen.queryByText(/done today/)).not.toBeInTheDocument();
    });
  });

  describe('CompletedRow reopen interaction', () => {
    it('clicking ✓ on a completed row fires updateTask(id, { status: "todo" })', async () => {
      const user = userEvent.setup();
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [COMPLETED_TASK],
      });
      // Second call after invalidateQueries — return empty to avoid re-rendering completed rows
      vi.mocked(fetchMyDay).mockResolvedValueOnce({
        ...BASE_MY_DAY,
        tasks: [COMPLETED_TASK],
      });

      renderSubject();

      // First expand the completed section
      const toggleBtn = await screen.findByText(/1 done today/);
      await user.click(toggleBtn);

      // The ✓ (Reopen task) button should now be visible
      const reopenBtn = screen.getByRole('button', { name: 'Reopen task' });
      await user.click(reopenBtn);

      expect(vi.mocked(updateTask)).toHaveBeenCalledOnce();
      expect(vi.mocked(updateTask)).toHaveBeenCalledWith(COMPLETED_TASK.id, { status: 'todo' });
    });
  });

  describe('CompletedRow title styling', () => {
    it('completed task title element includes text-meta class', async () => {
      const user = userEvent.setup();
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [COMPLETED_TASK],
      });

      renderSubject();

      // Expand the completed section
      const toggleBtn = await screen.findByText(/1 done today/);
      await user.click(toggleBtn);

      // The title span should carry text-meta (the faded style class)
      const titleEl = screen.getByText(COMPLETED_TASK.title);
      expect(titleEl.className).toContain('text-meta');
    });
  });

  describe('CompletedRow reopen toast notifications', () => {
    it('shows success toast when reopen mutation succeeds', async () => {
      const user = userEvent.setup();
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [COMPLETED_TASK],
      });
      vi.mocked(updateTask).mockResolvedValue(undefined as any);

      renderSubject();

      // Expand the completed section
      const toggleBtn = await screen.findByText(/1 done today/);
      await user.click(toggleBtn);

      // Click the reopen button
      const reopenBtn = screen.getByRole('button', { name: 'Reopen task' });
      await user.click(reopenBtn);

      // updateTask should have been called with the correct args
      expect(vi.mocked(updateTask)).toHaveBeenCalledWith(COMPLETED_TASK.id, { status: 'todo' });

      // Toast success should have fired
      expect(vi.mocked(toast.success)).toHaveBeenCalledWith('Task reopened');
    });

    it('shows error toast when reopen mutation fails', async () => {
      const user = userEvent.setup();
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [COMPLETED_TASK],
      });
      vi.mocked(updateTask).mockRejectedValue(new Error('Network error'));

      renderSubject();

      // Expand the completed section
      const toggleBtn = await screen.findByText(/1 done today/);
      await user.click(toggleBtn);

      // Click the reopen button
      const reopenBtn = screen.getByRole('button', { name: 'Reopen task' });
      await user.click(reopenBtn);

      // Toast error should have fired with the error message
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        'Failed to reopen: Network error',
      );
    });
  });

  describe('IntentSection auto-collapse when no completed tasks remain', () => {
    it('hides the completed footer when completedToday drops to zero after refetch', async () => {
      const user = userEvent.setup();

      // First fetch: 1 completed task (so the toggle appears)
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [COMPLETED_TASK],
      });

      const queryClient = createTestQueryClient();
      const { rerender } = renderWithProviders(
        <MemoryRouter>
          <IntentSection />
        </MemoryRouter>,
        { queryClient },
      );

      // Expand the completed section
      const toggleBtn = await screen.findByText(/1 done today/);
      await user.click(toggleBtn);

      // "Hide completed" should now be visible (showCompleted=true)
      expect(screen.getByText(/Hide completed/)).toBeInTheDocument();

      // Now simulate refetch returning 0 completed tasks
      vi.mocked(fetchMyDay).mockResolvedValue({
        ...BASE_MY_DAY,
        tasks: [],
      });

      // Invalidate queries to trigger a refetch
      await queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() });

      // After the useEffect fires (completedToday.length === 0 → setShowCompleted(false)),
      // the entire completed footer is gone (guarded by completedToday.length > 0)
      await screen.findByText(/No tasks for today/);
      expect(screen.queryByText(/Hide completed/)).not.toBeInTheDocument();
      expect(screen.queryByText(/done today/)).not.toBeInTheDocument();

      rerender(<></>);
    });
  });
});

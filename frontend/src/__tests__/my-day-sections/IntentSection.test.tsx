import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { IntentSection } from '@/components/my-day/sections/IntentSection';
import type { MyDayResponse } from '@/types';

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
}));

const { fetchMyDay, updateTask } = await import('@/lib/api');

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

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <IntentSection />
      </MemoryRouter>
    </QueryClientProvider>,
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

      renderWithProviders();

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

      renderWithProviders();

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

      renderWithProviders();

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

      renderWithProviders();

      // Expand the completed section
      const toggleBtn = await screen.findByText(/1 done today/);
      await user.click(toggleBtn);

      // The title span should carry text-meta (the faded style class)
      const titleEl = screen.getByText(COMPLETED_TASK.title);
      expect(titleEl.className).toContain('text-meta');
    });
  });
});

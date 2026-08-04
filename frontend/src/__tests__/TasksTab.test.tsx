import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { TasksTab } from '@/components/projects/TasksTab';
import type { Task } from '@/types';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchTasks: vi.fn(),
    createTask: vi.fn(),
    updateTask: vi.fn(),
    deleteTask: vi.fn(),
  };
});

import { fetchTasks } from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
const mockFetchTasks = vi.mocked(fetchTasks);

const BASE_TASK: Task = {
  id: 1,
  project_id: 1,
  parent_task_id: null,
  title: 'First task',
  description: null,
  status: 'todo',
  priority: 3,
  deadline: null,
  estimated_hours: null,
  actual_hours: null,
  sort_order: 0,
  completed_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const MOCK_TASKS: Task[] = [
  { ...BASE_TASK, id: 1, title: 'First task', status: 'todo', priority: 3, sort_order: 0 },
  { ...BASE_TASK, id: 2, title: 'Second task', status: 'done', priority: 2, sort_order: 1 },
  { ...BASE_TASK, id: 3, title: 'Third task', status: 'in_progress', priority: 1, sort_order: 2 },
];

function renderTab(projectId = 1) {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <TasksTab projectId={projectId} />,
    { queryClient },
  );
}

describe('TasksTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows task count meta text without § TASKS marker', async () => {
    mockFetchTasks.mockResolvedValue(MOCK_TASKS);

    renderTab();

    await waitFor(() => {
      expect(screen.getByText(/3 tasks/i)).toBeVisible();
    });
    expect(screen.queryByText('§ TASKS')).toBeNull();
  });

  it('shows singular "1 task" when only one task exists', async () => {
    mockFetchTasks.mockResolvedValue([{ ...BASE_TASK, id: 1, title: 'Only task', status: 'todo', priority: 3, sort_order: 0 }]);

    renderTab();

    await waitFor(() => {
      expect(screen.getByText(/1 task/i)).toBeVisible();
    });
    expect(screen.queryByText('§ TASKS')).toBeNull();
  });

  it('shows the "No tasks" empty state when the list loads empty', async () => {
    mockFetchTasks.mockResolvedValue([]);

    renderTab();

    expect(await screen.findByText('No tasks')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load tasks.')).toBeNull();
  });

  it('shows an error message, not the empty state, when tasks fail to load', async () => {
    mockFetchTasks.mockRejectedValue(new Error('network down'));

    renderTab();

    expect(await screen.findByText('Failed to load tasks.')).toBeInTheDocument();
    expect(screen.queryByText('No tasks')).toBeNull();
    expect(screen.queryByText(/0 tasks/)).toBeNull();
  });
});

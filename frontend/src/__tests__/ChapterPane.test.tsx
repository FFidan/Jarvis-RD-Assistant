import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ChapterPane } from '@/components/projects/ChapterPane';
import type { Project } from '@/types';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchProjectQuestions: vi.fn(),
    fetchProjectActivity: vi.fn(),
    fetchMilestones: vi.fn(),
    fetchTasks: vi.fn(),
    fetchProjectPapers: vi.fn(),
  };
});

import {
  fetchProjectQuestions,
  fetchProjectActivity,
  fetchMilestones,
  fetchTasks,
} from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const mockFetchQuestions = vi.mocked(fetchProjectQuestions);
const mockFetchActivity = vi.mocked(fetchProjectActivity);
const mockFetchMilestones = vi.mocked(fetchMilestones);
const mockFetchTasks = vi.mocked(fetchTasks);

const MOCK_PROJECT: Project = {
  id: 1,
  name: 'Test Chapter',
  description: null,
  status: 'active',
  deadline: null,
  color: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  paper_count: 0,
  open_question_count: 0,
};

function renderPane(project: Project | null = MOCK_PROJECT) {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <ChapterPane project={project} onDeleted={vi.fn()} />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('ChapterPane', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchQuestions.mockResolvedValue([]);
    mockFetchActivity.mockResolvedValue([]);
    mockFetchMilestones.mockResolvedValue([]);
    mockFetchTasks.mockResolvedValue([]);
  });

  it('shows error state when milestones query fails', async () => {
    mockFetchMilestones.mockRejectedValue(new Error('network error'));

    renderPane();

    await waitFor(() => {
      expect(screen.getByText(/failed to load milestones/i)).toBeInTheDocument();
    });
  });

  it('shows error state when questions query fails', async () => {
    mockFetchQuestions.mockRejectedValue(new Error('network error'));

    renderPane();

    await waitFor(() => {
      expect(screen.getByText(/failed to load questions/i)).toBeInTheDocument();
    });
  });

  it('shows error state when activity query fails', async () => {
    mockFetchActivity.mockRejectedValue(new Error('network error'));

    renderPane();

    await waitFor(() => {
      expect(screen.getByText(/failed to load activity/i)).toBeInTheDocument();
    });
  });

  it('shows error state when tasks query fails', async () => {
    mockFetchTasks.mockRejectedValue(new Error('network error'));

    renderPane();

    await waitFor(() => {
      expect(screen.getByText(/failed to load tasks/i)).toBeInTheDocument();
    });
  });

  it('renders empty state when project is null', () => {
    renderPane(null);
    expect(screen.getByText(/select a project/i)).toBeInTheDocument();
  });

  it('shows section headings with resolved data counts when project is supplied', async () => {
    const taskDefaults = {
      parent_task_id: null,
      description: null,
      priority: 0,
      deadline: null,
      estimated_hours: null,
      actual_hours: null,
      sort_order: 0,
      completed_at: null,
      updated_at: '2026-01-01T00:00:00Z',
    };
    mockFetchTasks.mockResolvedValue([
      { id: 1, title: 'Task A', status: 'todo', project_id: 1, created_at: '2026-01-01T00:00:00Z', ...taskDefaults },
      { id: 2, title: 'Task B', status: 'in_progress', project_id: 1, created_at: '2026-01-01T00:00:00Z', ...taskDefaults },
      { id: 3, title: 'Task C', status: 'done', project_id: 1, created_at: '2026-01-01T00:00:00Z', ...taskDefaults },
    ]);
    mockFetchMilestones.mockResolvedValue([
      { id: 1, name: 'M1', project_id: 1, completed: false, deadline: null, description: null, completed_at: null, created_at: '2026-01-01T00:00:00Z' },
      { id: 2, name: 'M2', project_id: 1, completed: true, deadline: null, description: null, completed_at: '2026-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z' },
    ]);
    mockFetchQuestions.mockResolvedValue([
      { id: 1, body: 'Q1?', project_id: 1, created_at: '2026-01-01T00:00:00Z' },
    ]);
    mockFetchActivity.mockResolvedValue([]);

    renderPane();

    await waitFor(() => {
      expect(screen.getByText(/TASKS · 3/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/MILESTONES · 2/i)).toBeInTheDocument();
  });
});

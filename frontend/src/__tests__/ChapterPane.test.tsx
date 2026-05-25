import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChapterPane project={project} onDeleted={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
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
    expect(screen.getByText(/select a chapter/i)).toBeInTheDocument();
  });
});

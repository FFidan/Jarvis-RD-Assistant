import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ProjectsPage } from '@/pages/ProjectsPage';

// Mock API module — include all functions called by the new IA components
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchProjects: vi.fn(),
    fetchTasks: vi.fn(),
    fetchMilestones: vi.fn(),
    fetchProjectQuestions: vi.fn(),
    fetchProjectActivity: vi.fn(),
    fetchProjectPapers: vi.fn(),
  };
});

import {
  fetchProjects,
  fetchProjectQuestions,
  fetchProjectActivity,
  fetchTasks,
  fetchMilestones,
  fetchProjectPapers,
} from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
const mockFetchProjects = vi.mocked(fetchProjects);
const mockFetchQuestions = vi.mocked(fetchProjectQuestions);
const mockFetchActivity = vi.mocked(fetchProjectActivity);
const mockFetchTasks = vi.mocked(fetchTasks);
const mockFetchMilestones = vi.mocked(fetchMilestones);
const mockFetchPapers = vi.mocked(fetchProjectPapers);

function renderPage() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <ProjectsPage />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('ProjectsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default safe stubs for the IA sub-components
    mockFetchQuestions.mockResolvedValue([]);
    mockFetchActivity.mockResolvedValue([]);
    mockFetchTasks.mockResolvedValue([]);
    mockFetchMilestones.mockResolvedValue([]);
    mockFetchPapers.mockResolvedValue([]);
  });

  it('renders project list with projects', async () => {
    mockFetchProjects.mockResolvedValue([
      {
        id: 1,
        name: 'Test Project',
        description: 'A test project',
        status: 'active',
        deadline: '2026-06-01',
        color: null,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        paper_count: 0,
        open_question_count: 0,
      },
    ]);

    renderPage();

    await waitFor(() => {
      // Project name appears in the chapter rail row
      expect(screen.getAllByText('Test Project').length).toBeGreaterThan(0);
    });
    // status chips use translated labels
    expect(screen.getAllByText('In progress').length).toBeGreaterThan(0);
  });

  it('shows empty state in rail when no projects exist', async () => {
    mockFetchProjects.mockResolvedValue([]);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('No projects yet')).toBeInTheDocument();
    });
    expect(
      screen.getByText('Create a project to start organizing your research.'),
    ).toBeInTheDocument();
  });

  it('auto-selects first project and shows chapter pane (not "select a project" prompt)', async () => {
    mockFetchProjects.mockResolvedValue([
      {
        id: 1,
        name: 'My Project',
        description: null,
        status: 'active',
        deadline: null,
        color: null,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        paper_count: 0,
        open_question_count: 0,
      },
    ]);

    renderPage();

    // With auto-select, the "Select a project" empty state should NOT appear
    await waitFor(() => {
      // Breadcrumb contains the project name
      expect(screen.getAllByText('My Project').length).toBeGreaterThan(0);
    });
    expect(screen.queryByText('Select a project')).not.toBeInTheDocument();
  });

  it('shows PROJECTS header in the chapter rail', async () => {
    mockFetchProjects.mockResolvedValue([]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/PROJECTS · 0/i)).toBeInTheDocument();
    });
  });

  it('shows QueryErrorState (not empty 2-pane) when fetchProjects rejects', async () => {
    mockFetchProjects.mockRejectedValue(new Error('server error'));
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/couldn't load/i)).toBeInTheDocument();
    });
    // 2-pane layout must NOT be rendered
    expect(screen.queryByText(/PROJECTS ·/i)).not.toBeInTheDocument();
  });

  it('empty projects shows 2-pane layout (not error UI)', async () => {
    mockFetchProjects.mockResolvedValue([]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/PROJECTS · 0/i)).toBeInTheDocument();
    });
    // A successful empty response must not show the error UI
    expect(screen.queryByText(/couldn't load/i)).not.toBeInTheDocument();
  });
});

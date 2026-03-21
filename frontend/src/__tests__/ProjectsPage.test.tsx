import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ProjectsPage } from '@/pages/ProjectsPage';

// Mock API module
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchProjects: vi.fn(),
    fetchTasks: vi.fn(),
    fetchMilestones: vi.fn(),
  };
});

import { fetchProjects } from '@/lib/api';
const mockFetchProjects = vi.mocked(fetchProjects);

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ProjectsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ProjectsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
      },
    ]);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Test Project')).toBeInTheDocument();
    });
    expect(screen.getByText('active')).toBeInTheDocument();
  });

  it('shows empty state when no projects exist', async () => {
    mockFetchProjects.mockResolvedValue([]);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('No projects yet')).toBeInTheDocument();
    });
    expect(screen.getByText('Create a project to organize papers and track research goals.')).toBeInTheDocument();
  });

  it('shows select prompt in detail panel when no project is selected', async () => {
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
      },
    ]);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Select a project')).toBeInTheDocument();
    });
  });
});

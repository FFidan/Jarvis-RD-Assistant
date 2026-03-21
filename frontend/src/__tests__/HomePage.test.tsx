import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HomePage } from '@/pages/HomePage';

// Mock the api module
vi.mock('@/lib/api', () => ({
  fetchDashboardMetrics: vi.fn(),
  checkHealth: vi.fn(),
}));

const { fetchDashboardMetrics } = await import('@/lib/api');

function renderHomePage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mockMetrics = {
  total_papers: 42,
  unread_papers: 7,
  pending_papers: 3,
  due_cards: 5,
  active_projects: 2,
  topic_count: 4,
  nudge_count: 6,
  onboarding_stage: 'complete',
};

describe('HomePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the dashboard heading', () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
  });

  it('shows skeleton loaders while loading', () => {
    vi.mocked(fetchDashboardMetrics).mockReturnValue(new Promise(() => {}));
    const { container } = renderHomePage();
    // Skeleton elements have the animate-pulse class
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders metric tiles when data loads', async () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    // Wait for data to render
    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(screen.getByText('Total Papers')).toBeInTheDocument();
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('Unread Papers')).toBeInTheDocument();
  });

  it('renders quick navigation links', () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.getByText('Quick Navigation')).toBeInTheDocument();
    expect(screen.getByText('Research Feed')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('renders all seven metric tiles when data loads', async () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(await screen.findByText('Total Papers')).toBeInTheDocument();
    expect(screen.getByText('Unread Papers')).toBeInTheDocument();
    expect(screen.getByText('Pending Papers')).toBeInTheDocument();
    expect(screen.getByText('Due Cards')).toBeInTheDocument();
    expect(screen.getByText('Active Projects')).toBeInTheDocument();
    expect(screen.getByText('Topics')).toBeInTheDocument();
    expect(screen.getByText('Nudges')).toBeInTheDocument();
  });

  it('renders all quick navigation links', async () => {
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(mockMetrics);
    renderHomePage();
    expect(screen.getByText('Research Feed')).toBeInTheDocument();
    expect(screen.getByText('Analytics')).toBeInTheDocument();
    expect(screen.getByText('Projects')).toBeInTheDocument();
    expect(screen.getByText('Learning Cards')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByText('Citation Graph')).toBeInTheDocument();
    expect(screen.getByText('Knowledge Graph')).toBeInTheDocument();
    expect(screen.getByText('Extraction Table')).toBeInTheDocument();
  });

  it('renders zero values when metrics are all zeros', async () => {
    const zeroMetrics = {
      total_papers: 0,
      unread_papers: 0,
      pending_papers: 0,
      due_cards: 0,
      active_projects: 0,
      topic_count: 0,
      nudge_count: 0,
    };
    vi.mocked(fetchDashboardMetrics).mockResolvedValue(zeroMetrics);
    renderHomePage();
    expect(await screen.findByText('Total Papers')).toBeInTheDocument();
    // All seven tiles should show 0
    const zeros = screen.getAllByText('0');
    expect(zeros.length).toBe(7);
  });
});

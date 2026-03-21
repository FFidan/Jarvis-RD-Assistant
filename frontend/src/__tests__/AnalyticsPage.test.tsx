import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AnalyticsPage } from '@/pages/AnalyticsPage';

// Mock the API module
vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchAnalyticsActivity: vi.fn().mockResolvedValue([
      { log_date: '2026-03-01', tasks_completed: 2, cards_reviewed: 5, papers_read: 1, focus_hours: 3, notes: null },
    ]),
    fetchAnalyticsRetention: vi.fn().mockResolvedValue([
      { review_date: '2026-03-01', total: 10, good_easy: 8, retention_pct: 80.0 },
    ]),
    fetchAnalyticsReviews: vi.fn().mockResolvedValue([
      { rating: 3, count: 15 },
      { rating: 4, count: 10 },
    ]),
    fetchAnalyticsLlmCost: vi.fn().mockResolvedValue([
      { day: '2026-03-01', total_cost: 0.05, workflow: 'summarize' },
    ]),
    fetchPapersBySource: vi.fn().mockResolvedValue([
      { source_type: 'arxiv', count: 10 },
      { source_type: 'local', count: 5 },
    ]),
    fetchPapersByStatus: vi.fn().mockResolvedValue([
      { status: 'new', count: 8 },
      { status: 'read', count: 7 },
    ]),
  };
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AnalyticsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AnalyticsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the page title', () => {
    renderPage();
    expect(screen.getByText('Analytics')).toBeInTheDocument();
  });

  it('renders all six chart cards', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Activity Overview')).toBeInTheDocument();
      expect(screen.getByText('Retention Trend')).toBeInTheDocument();
      expect(screen.getByText('Papers by Source')).toBeInTheDocument();
      expect(screen.getByText('Papers by Status')).toBeInTheDocument();
      expect(screen.getByText('Reviews by Rating')).toBeInTheDocument();
      expect(screen.getByText('LLM Cost Over Time')).toBeInTheDocument();
    });
  });

  it('renders date range filter with preset buttons', () => {
    renderPage();
    expect(screen.getByText('Last 7 days')).toBeInTheDocument();
    expect(screen.getByText('Last 30 days')).toBeInTheDocument();
    expect(screen.getByText('Last 90 days')).toBeInTheDocument();
  });

  it('changes date range when clicking preset', async () => {
    const api = await import('@/lib/api');
    renderPage();
    fireEvent.click(screen.getByText('Last 7 days'));
    await waitFor(() => {
      expect(api.fetchAnalyticsActivity).toHaveBeenCalledWith(7);
    });
  });
});

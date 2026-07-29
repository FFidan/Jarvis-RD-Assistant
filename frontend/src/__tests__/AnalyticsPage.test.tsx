/**
 * AnalyticsPage unit tests — covers the Analytics IA:
 *  - Breadcrumb ("Learn" group), hero "Analytics", period subtitle
 *  - section markers (REVIEW · N DAYS, READING CADENCE, LIBRARY, REVIEWS, COST)
 *  - KPI band renders via summaryQuery
 *  - DateRangeFilter drives days param into summary + chart queries
 *  - All six existing chart cards still render (regression guard)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AnalyticsPage } from '@/pages/AnalyticsPage';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// Mock the API module — includes fetchAnalyticsSummary + all existing chart fns.
vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchAnalyticsSummary: vi.fn().mockResolvedValue({
      papers_read_total: 24,
      focus_hours_total: 37.2,
      cards_reviewed_total: 412,
      papers_read_prev: 18,
      focus_hours_prev: 41.3,
      cards_reviewed_prev: 380,
      focus_streak_days: 5,
      cards_review_streak_days: 28,
    }),
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
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <AnalyticsPage />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('AnalyticsPage — Analytics IA', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ── Hero identity ────────────────────────────────────────────────────────

  it('renders the Analytics hero heading', () => {
    renderPage();
    // h1 "Analytics" (the page name) — use role to avoid ambiguity with the breadcrumb span
    expect(screen.getByRole('heading', { name: 'Analytics' })).toBeInTheDocument();
  });

  it('renders the breadcrumb with Analytics link', () => {
    renderPage();
    // breadcrumb "Analytics" is an anchor link
    expect(screen.getByRole('link', { name: 'Analytics' })).toBeInTheDocument();
    // breadcrumb group "Learn" (the real sidebar group for Analytics) appears in the nav
    const nav = screen.getByRole('navigation');
    expect(nav).toHaveTextContent('Learn');
  });

  it('renders REVIEW · 30 DAYS marker with default days', () => {
    renderPage();
    expect(screen.getByText(/REVIEW · 30 DAYS/i)).toBeInTheDocument();
  });

  it('renders the italic period subtitle containing "since"', () => {
    renderPage();
    // The subtitle renders "since {date}." across child text nodes.
    // Use a function matcher to search the paragraph's full textContent.
    const el = screen.getByText((_, element) =>
      (element?.tagName === 'P') &&
      (element.textContent ?? '').includes('What you learned') &&
      (element.textContent ?? '').includes('since'),
    );
    expect(el).toBeInTheDocument();
  });

  // ── Section markers ──────────────────────────────────────────────────────

  it('renders READING CADENCE section marker', () => {
    renderPage();
    expect(screen.getByText(/READING CADENCE/i)).toBeInTheDocument();
  });

  it('renders LIBRARY section marker', () => {
    renderPage();
    expect(screen.getByText(/LIBRARY/i)).toBeInTheDocument();
  });

  it('renders REVIEWS section marker', () => {
    renderPage();
    // MarkerCaption renders "REVIEWS" inside the eyebrow span.
    const el = screen.getByText((_, element) =>
      element?.tagName === 'SPAN' &&
      (element.textContent ?? '') === 'REVIEWS',
    );
    expect(el).toBeInTheDocument();
  });

  it('renders COST section marker', () => {
    renderPage();
    // Same pattern — span textContent "COST".
    const el = screen.getByText((_, element) =>
      element?.tagName === 'SPAN' &&
      (element.textContent ?? '') === 'COST',
    );
    expect(el).toBeInTheDocument();
  });

  // ── KPI band ─────────────────────────────────────────────────────────────

  it('renders KPI band with PAPERS READ label', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('PAPERS READ')).toBeInTheDocument();
    });
  });

  it('renders KPI band with FOCUS HOURS label', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('FOCUS HOURS')).toBeInTheDocument();
    });
  });

  it('renders KPI band with CARDS REVIEWED label', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('CARDS REVIEWED')).toBeInTheDocument();
    });
  });

  it('renders PAPERS READ total (24)', async () => {
    renderPage();
    await waitFor(() => {
      const values = screen.getAllByTestId('kpi-value');
      const papersCell = values.find((el) => el.textContent === '24');
      expect(papersCell).toBeTruthy();
    });
  });

  it('renders positive papers delta as green +N vs prev', async () => {
    renderPage();
    await waitFor(() => {
      // papers_read_total 24 − prev 18 = +6
      const chip = screen.getAllByTestId('trend-chip').find(
        (el) => el.textContent?.includes('+6'),
      );
      expect(chip).toBeTruthy();
    });
  });

  it('renders negative focus hours delta as amber −N vs prev', async () => {
    renderPage();
    await waitFor(() => {
      // focus 37.2 − 41.3 = −4.1
      const chip = screen.getAllByTestId('trend-chip').find(
        (el) => el.textContent?.includes('-4.1') || el.textContent?.includes('−4.1'),
      );
      expect(chip).toBeTruthy();
    });
  });

  it('renders streak badge for CARDS REVIEWED when streak > 0', async () => {
    renderPage();
    await waitFor(() => {
      const streakChips = screen.getAllByTestId('streak-chip');
      expect(streakChips.length).toBeGreaterThan(0);
      expect(streakChips[0]?.textContent).toContain('28-day streak');
    });
  });

  // ── DateRangeFilter drives days param ────────────────────────────────────

  it('renders date range filter with preset buttons', () => {
    renderPage();
    expect(screen.getByText('Last 7 days')).toBeInTheDocument();
    expect(screen.getByText('Last 30 days')).toBeInTheDocument();
    expect(screen.getByText('Last 90 days')).toBeInTheDocument();
  });

  it('clicking Last 7 days passes days=7 into summary + activity fetch', async () => {
    const api = await import('@/lib/api');
    renderPage();
    fireEvent.click(screen.getByText('Last 7 days'));
    await waitFor(() => {
      expect(api.fetchAnalyticsSummary).toHaveBeenCalledWith(7);
      expect(api.fetchAnalyticsActivity).toHaveBeenCalledWith(7);
    });
  });

  it('REVIEW marker updates to · 7 DAYS after clicking Last 7 days', async () => {
    renderPage();
    fireEvent.click(screen.getByText('Last 7 days'));
    await waitFor(() => {
      expect(screen.getByText(/REVIEW · 7 DAYS/i)).toBeInTheDocument();
    });
  });

  // ── Regression guard: existing chart cards still render ──────────────────

  it('renders all six chart card titles (regression guard)', async () => {
    renderPage();
    await waitFor(() => {
      // ActivityChart is now titled "Daily Activity"
      expect(screen.getByText('Daily Activity')).toBeInTheDocument();
      expect(screen.getByText('Retention Trend')).toBeInTheDocument();
      expect(screen.getByText('Papers by Source')).toBeInTheDocument();
      expect(screen.getByText('Papers by Status')).toBeInTheDocument();
      expect(screen.getByText('Reviews by Rating')).toBeInTheDocument();
      expect(screen.getByText('LLM Cost Over Time')).toBeInTheDocument();
    });
  });
});

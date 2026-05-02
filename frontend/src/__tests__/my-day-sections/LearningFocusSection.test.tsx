import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { LearningFocusSection } from '@/components/my-day/sections/LearningFocusSection';
import type { MyDayResponse, RetentionStats } from '@/types';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const startWorkMock = vi.fn();

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (state: any) => any) => {
      const state = { phase: 'idle', attachedItem: null };
      return selector ? selector(state) : state;
    },
    { getState: () => ({ startWork: startWorkMock }) },
  ),
}));

vi.mock('@/lib/api', () => ({
  getStats: vi.fn(),
  fetchMyDay: vi.fn(),
}));

const { getStats, fetchMyDay } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

const MOCK_MY_DAY: MyDayResponse = {
  tasks: [],
  cards_due: 0,
  recommendations: [],
  today_focus_hours: 1.5,
  focus_streak_days: 3,
  project_pulse: [],
};

function makeStats(due_now: number): RetentionStats {
  return {
    total_cards: 50,
    due_now,
    reviewed_today: 2,
    average_retention: 0.85,
    reviews_by_rating: {},
    streak_days: 5,
  };
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <LearningFocusSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('LearningFocusSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchMyDay).mockResolvedValue(MOCK_MY_DAY);
  });

  it('shows "Review now →" button when due_now > 0', async () => {
    vi.mocked(getStats).mockResolvedValue(makeStats(5));

    renderWithProviders();

    expect(await screen.findByText('Review now →')).toBeInTheDocument();
    // The "no reviews" message should NOT appear
    expect(screen.queryByText(/No reviews pending/i)).not.toBeInTheDocument();
  });

  it('shows "No reviews pending. ✓" text when due_now === 0', async () => {
    vi.mocked(getStats).mockResolvedValue(makeStats(0));

    renderWithProviders();

    expect(await screen.findByText('No reviews pending. ✓')).toBeInTheDocument();
    // The "Review now →" button should NOT appear
    expect(screen.queryByText('Review now →')).not.toBeInTheDocument();
  });
});

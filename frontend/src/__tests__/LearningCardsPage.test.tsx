import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { LearningCardsPage } from '@/pages/LearningCardsPage';

// Mock API module
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getStats: vi.fn(),
    getNextReview: vi.fn(),
    fetchDecks: vi.fn(),
    fetchCards: vi.fn(),
  };
});

import { getStats, getNextReview, fetchDecks } from '@/lib/api';
const mockGetStats = vi.mocked(getStats);
const mockGetNextReview = vi.mocked(getNextReview);
const mockFetchDecks = vi.mocked(fetchDecks);

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <LearningCardsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('LearningCardsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetNextReview.mockResolvedValue([]);
    mockFetchDecks.mockResolvedValue([]);
  });

  it('renders stats header with data', async () => {
    mockGetStats.mockResolvedValue({
      total_cards: 42,
      due_now: 5,
      reviewed_today: 10,
      average_retention: 85.3,
      reviews_by_rating: { '3': 8, '4': 2 },
      streak_days: 7,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('42')).toBeInTheDocument();
    });
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('10')).toBeInTheDocument();
    expect(screen.getByText('85.3%')).toBeInTheDocument();
    expect(screen.getByText('7d')).toBeInTheDocument();
  });

  it('shows empty state when no cards are due', async () => {
    mockGetStats.mockResolvedValue({
      total_cards: 0,
      due_now: 0,
      reviewed_today: 0,
      average_retention: 0,
      reviews_by_rating: {},
      streak_days: 0,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('No cards to review')).toBeInTheDocument();
    });
    expect(screen.getByText("All caught up! Generate cards from a paper or wait for scheduled reviews.")).toBeInTheDocument();
  });

  it('renders page title and action buttons', () => {
    mockGetStats.mockResolvedValue({
      total_cards: 0,
      due_now: 0,
      reviewed_today: 0,
      average_retention: 0,
      reviews_by_rating: {},
      streak_days: 0,
    });

    renderPage();

    expect(screen.getByText('Learning Cards')).toBeInTheDocument();
    expect(screen.getByText('Generate')).toBeInTheDocument();
    expect(screen.getByText('New Card')).toBeInTheDocument();
  });

  it('shows Review and Browse tabs', () => {
    mockGetStats.mockResolvedValue({
      total_cards: 0,
      due_now: 0,
      reviewed_today: 0,
      average_retention: 0,
      reviews_by_rating: {},
      streak_days: 0,
    });

    renderPage();

    expect(screen.getByText('Review')).toBeInTheDocument();
    expect(screen.getByText('Browse')).toBeInTheDocument();
  });

  it('does not render § REVIEW section marker in the Review tab', () => {
    mockGetStats.mockResolvedValue({
      total_cards: 0,
      due_now: 0,
      reviewed_today: 0,
      average_retention: 0,
      reviews_by_rating: {},
      streak_days: 0,
    });

    renderPage();

    expect(screen.queryByText(/§\s*REVIEW/)).toBeNull();
  });
});

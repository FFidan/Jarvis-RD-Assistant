import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { LearningCardsSummary } from '@/components/my-day/LearningCardsSummary';
import * as api from '@/lib/api';
import type { RetentionStats } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getStats: vi.fn(),
  };
});

const statsWithDue: RetentionStats = {
  total_cards: 100,
  due_now: 8,
  reviewed_today: 2,
  average_retention: 0.82,
  reviews_by_rating: {},
  streak_days: 5,
};

const statsNothingDue: RetentionStats = {
  total_cards: 100,
  due_now: 0,
  reviewed_today: 0,
  average_retention: 0.9,
  reviews_by_rating: {},
  streak_days: 12,
};

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <LearningCardsSummary />
      </BrowserRouter>
    </QueryClientProvider>,
  );
}

describe('LearningCardsSummary', () => {
  it('shows cards due count and Review Now button', async () => {
    vi.mocked(api.getStats).mockResolvedValue(statsWithDue);
    renderWithProviders();
    expect(await screen.findByText('8')).toBeInTheDocument();
    expect(screen.getByText('Review Now')).toBeInTheDocument();
  });

  it('shows streak days', async () => {
    vi.mocked(api.getStats).mockResolvedValue(statsWithDue);
    renderWithProviders();
    expect(await screen.findByText('5 day streak')).toBeInTheDocument();
  });

  it('shows reviewed today count', async () => {
    vi.mocked(api.getStats).mockResolvedValue(statsWithDue);
    renderWithProviders();
    expect(await screen.findByText('2 reviewed today')).toBeInTheDocument();
  });

  it('hides Review Now button when nothing due', async () => {
    vi.mocked(api.getStats).mockResolvedValue(statsNothingDue);
    renderWithProviders();
    // streak days appears after load
    expect(await screen.findByText('12 day streak')).toBeInTheDocument();
    expect(screen.queryByText('Review Now')).not.toBeInTheDocument();
  });

  it('shows "No reviews pending" when due_now is 0', async () => {
    vi.mocked(api.getStats).mockResolvedValue(statsNothingDue);
    renderWithProviders();
    expect(await screen.findByText('No reviews pending.')).toBeInTheDocument();
  });

  it('renders skeleton while loading', () => {
    vi.mocked(api.getStats).mockReturnValue(new Promise(() => {}));
    renderWithProviders();
    const skeletons = document.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });
});

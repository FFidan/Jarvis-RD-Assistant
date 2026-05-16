/**
 * Unit tests for SessionComplete — session-end panel.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { SessionComplete } from '@/components/cards/SessionComplete';
import type { RetentionStats } from '@/types';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, getStats: vi.fn() };
});

import { getStats } from '@/lib/api';
const mockGetStats = vi.mocked(getStats);

const STATS_FIXTURE: RetentionStats = {
  total_cards: 50,
  due_now: 0,
  reviewed_today: 12,
  average_retention: 87.5,
  reviews_by_rating: { '3': 8, '4': 4 },
  streak_days: 5,
};

function renderComplete(sessionReviewed = 7, onNav = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SessionComplete sessionReviewed={sessionReviewed} onNavigateToLibrary={onNav} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('SessionComplete', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetStats.mockResolvedValue(STATS_FIXTURE);
  });

  it('shows "Session complete" heading', async () => {
    renderComplete();
    await waitFor(() => expect(screen.getByText(/session complete/i)).toBeInTheDocument());
  });

  it('shows session reviewed count', async () => {
    renderComplete(7);
    await waitFor(() => expect(screen.getByText(/reviewed 7 cards/i)).toBeInTheDocument());
  });

  it('shows streak from RetentionStats', async () => {
    renderComplete();
    await waitFor(() => expect(screen.getByText('5d')).toBeInTheDocument());
  });

  it('shows reviewed today from RetentionStats', async () => {
    renderComplete();
    await waitFor(() => expect(screen.getByText('12')).toBeInTheDocument());
  });

  it('shows retention percentage', async () => {
    renderComplete();
    await waitFor(() => expect(screen.getByText('87.5%')).toBeInTheDocument());
  });

  it('calls onNavigateToLibrary when "Manage library" clicked', async () => {
    const spy = vi.fn();
    renderComplete(5, spy);
    await waitFor(() => screen.getByText(/manage library/i));
    await userEvent.click(screen.getByRole('button', { name: /manage library/i }));
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('uses singular "card" for sessionReviewed = 1', async () => {
    renderComplete(1);
    await waitFor(() => expect(screen.getByText(/reviewed 1 card this session/i)).toBeInTheDocument());
  });

  it('hides session count when sessionReviewed = 0', async () => {
    renderComplete(0);
    await waitFor(() => screen.getByText(/session complete/i));
    expect(screen.queryByText(/reviewed 0/i)).toBeNull();
  });
});

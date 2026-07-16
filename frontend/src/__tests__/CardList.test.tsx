/**
 * Tests for CardList — verifies onError toast fires when deleteCard fails.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CardList } from '@/components/cards/CardList';
import type { Card } from '@/types';

vi.mock('@/lib/api', () => ({
  fetchCards: vi.fn(),
  deleteCard: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import { fetchCards, deleteCard } from '@/lib/api';
import { toast } from 'sonner';

const mockFetchCards = vi.mocked(fetchCards);
const mockDeleteCard = vi.mocked(deleteCard);

const CARD: Card = {
  id: 7,
  deck_id: 1,
  paper_id: null,
  card_type: 'concept',
  front: 'What is entropy?',
  back: 'A measure of disorder.',
  evidence: null,
  fsrs_state: {},
  due_at: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

const wrap = (ui: React.ReactNode) => {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
};

describe('CardList — fetchCards failure', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the error state (not the empty-state) and Retry recovers the card table', async () => {
    mockFetchCards
      .mockRejectedValueOnce(new Error('network failure'))
      .mockResolvedValueOnce([CARD]);

    wrap(<CardList deckId={1} />);

    // Failure renders an explicit error + Retry, never the misleading empty-state.
    expect(await screen.findByText(/check your connection and try again/i)).toBeInTheDocument();
    expect(screen.queryByText('No cards in this deck')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('What is entropy?')).toBeInTheDocument();
    expect(mockFetchCards).toHaveBeenCalledTimes(2);
  });
});

describe('CardList — deleteCard onError', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchCards.mockResolvedValue([CARD]);
  });

  it('fires toast.error when deleteCard mutation fails', async () => {
    mockDeleteCard.mockRejectedValue(new Error('network failure'));

    wrap(<CardList deckId={1} />);

    // Wait for the card row to appear, then click the icon button (Trash2) in that row
    await screen.findByText('What is entropy?');
    // The trash button is the only button rendered in the card list row
    await userEvent.click(screen.getAllByRole('button')[0]!);

    // ConfirmDialog appears with confirmLabel="Delete"
    const confirmBtn = await screen.findByRole('button', { name: /^delete$/i });
    await userEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockDeleteCard).toHaveBeenCalledWith(7);
    });
    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        'Failed to delete card. Please try again.',
      );
    });
  });
});

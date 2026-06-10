/**
 * Tests for DeckBrowser — verifies onError toast fires when createDeck fails.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { DeckBrowser } from '@/components/cards/DeckBrowser';

vi.mock('@/lib/api', () => ({
  fetchDecks: vi.fn(),
  createDeck: vi.fn(),
  exportAnki: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import { fetchDecks, createDeck } from '@/lib/api';
import { toast } from 'sonner';

const mockFetchDecks = vi.mocked(fetchDecks);
const mockCreateDeck = vi.mocked(createDeck);

const wrap = (ui: React.ReactNode) => {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
};

describe('DeckBrowser — createDeck onError', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue([]);
  });

  it('fires toast.error when createDeck mutation fails', async () => {
    mockCreateDeck.mockRejectedValue(new Error('server error'));

    wrap(<DeckBrowser selectedDeckId={null} onSelectDeck={vi.fn()} />);

    // Open the "New Deck" dialog
    await userEvent.click(await screen.findByRole('button', { name: /new deck/i }));

    // Fill in a name
    await userEvent.type(screen.getByLabelText(/name/i), 'My Deck');

    // Submit
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        'Failed to create deck. Please try again.',
      );
    });
  });
});

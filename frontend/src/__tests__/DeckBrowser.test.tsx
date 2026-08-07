/**
 * Tests for DeckBrowser — verifies onError toast fires when createDeck fails,
 * and that deck cards render without invalid DOM nesting and support keyboard activation.
 */
import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import userEvent from '@testing-library/user-event';
import { DeckBrowser } from '@/components/cards/DeckBrowser';
import type { Deck } from '@/types';

vi.mock('@/lib/api', () => ({
  fetchDecks: vi.fn(),
  createDeck: vi.fn(),
  exportAnki: vi.fn(),
}));
vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

import { fetchDecks, createDeck } from '@/lib/api';
import { toast } from 'sonner';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const mockFetchDecks = vi.mocked(fetchDecks);
const mockCreateDeck = vi.mocked(createDeck);

const makeDeck = (overrides: Partial<Deck> = {}): Deck => ({
  id: 1,
  name: 'Deck One',
  description: null,
  topic_id: null,
  card_count: 3,
  due_count: 2,
  created_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const wrap = (ui: React.ReactNode) => {
  const qc = createTestQueryClient();
  return renderWithProviders(
    ui,
    { queryClient: qc },
  );
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

describe('DeckBrowser — fetchDecks failure', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the error state (not the empty-state) and Retry recovers the deck grid', async () => {
    mockFetchDecks
      .mockRejectedValueOnce(new Error('network failure'))
      .mockResolvedValueOnce([makeDeck()]);

    wrap(<DeckBrowser selectedDeckId={null} onSelectDeck={vi.fn()} />);

    // Failure renders an explicit error + Retry, never the misleading empty-state.
    expect(await screen.findByText(/check your connection and try again/i)).toBeInTheDocument();
    expect(screen.queryByText('No flashcard decks yet')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('Deck One')).toBeInTheDocument();
    expect(mockFetchDecks).toHaveBeenCalledTimes(2);
  });
});

describe('DeckBrowser — deck card DOM and keyboard activation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue([makeDeck()]);
  });

  it('renders deck cards without a console error (no nested-button DOM)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    wrap(<DeckBrowser selectedDeckId={null} onSelectDeck={vi.fn()} />);
    await screen.findByText('Deck One');

    expect(consoleErrorSpy).not.toHaveBeenCalled();
    consoleErrorSpy.mockRestore();
  });

  it('activates deck selection on Enter and Space when the card is focused', async () => {
    const onSelectDeck = vi.fn();
    wrap(<DeckBrowser selectedDeckId={null} onSelectDeck={onSelectDeck} />);

    const card = (await screen.findByText('Deck One')).closest<HTMLElement>('[role="button"]');
    if (!card) throw new Error('deck card wrapper not found');

    card.focus();
    await userEvent.keyboard('{Enter}');
    expect(onSelectDeck).toHaveBeenCalledWith(1);

    onSelectDeck.mockClear();
    card.focus();
    await userEvent.keyboard(' ');
    expect(onSelectDeck).toHaveBeenCalledWith(1);
  });
});

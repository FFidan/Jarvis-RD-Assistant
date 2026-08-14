import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HeroPulse } from '@/components/my-day/sections/HeroPulse';
import type { PulseDeck, PulseCardItem } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (state: { phase: string; attachedItem: null }) => unknown) => {
      const state = { phase: 'idle', attachedItem: null };
      return selector ? selector(state) : state;
    },
    { getState: () => ({ startWork: vi.fn() }) },
  ),
}));

vi.mock('@/lib/api', () => ({
  fetchPulseToday: vi.fn(),
  ratePulseCard: vi.fn(),
}));

const { fetchPulseToday, ratePulseCard } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeCard(overrides: Partial<PulseCardItem> = {}): PulseCardItem {
  return {
    card_id: 1,
    paper_id: 100,
    paper_title: 'Default Paper',
    paper_authors: ['Author'],
    paper_url: null,
    rank: 1,
    score: 0.8,
    llm_relevance: null,
    llm_novelty: null,
    reasoning: null,
    reasoning_verified: null,
    reasoning_confidence: null,
    signals: {},
    user_state: null,
    tags: null,
    ...overrides,
  };
}

function makeDeck(overrides: Partial<PulseDeck> = {}): PulseDeck {
  return {
    deck_id: 1,
    deck_date: '2026-05-03',
    card_count: 1,
    generated_at: '2026-05-03T08:00:00Z',
    cards: [makeCard()],
    stats: {},
    degraded_reason: null,
    ...overrides,
  };
}

function renderHeroPulse(queryClient?: QueryClient) {
  const qc = queryClient ?? createTestQueryClient();
  return {
    qc,
    ...renderWithProviders(
      <MemoryRouter>
        <HeroPulse />
      </MemoryRouter>,
      { queryClient: qc },
    ),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('HeroPulse currentIndex clamp and reset', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(ratePulseCard).mockResolvedValue(undefined as any);
  });

  it('resets currentIndex to 0 when deck_id changes (deck regenerate)', async () => {
    const user = userEvent.setup();

    // Deck 1: two cards
    const deck1 = makeDeck({
      deck_id: 1,
      card_count: 2,
      cards: [
        makeCard({ card_id: 1, paper_id: 101, paper_title: 'Paper A', rank: 1 }),
        makeCard({ card_id: 2, paper_id: 102, paper_title: 'Paper B', rank: 2 }),
      ],
    });

    vi.mocked(fetchPulseToday).mockResolvedValue(deck1);

    const { qc } = renderHeroPulse();

    // Wait for deck 1 card 1 to render — "#1 of 2"
    expect(await screen.findByText(/#1 of 2/)).toBeInTheDocument();

    // Advance to card 2 by rating card 1 (Accept → onSuccess increments currentIndex)
    const acceptBtn = screen.getByRole('button', { name: 'Accept' });
    await user.click(acceptBtn);
    expect(await screen.findByText(/#2 of 2/)).toBeInTheDocument();

    // Simulate deck regeneration: new deck_id = 2, one card
    const deck2 = makeDeck({
      deck_id: 2,
      card_count: 1,
      cards: [
        makeCard({ card_id: 3, paper_id: 201, paper_title: 'Fresh Paper', rank: 1 }),
      ],
    });

    // Directly update the query cache to simulate a refetch returning deck2
    act(() => {
      qc.setQueryData<PulseDeck>(['pulse-today'], deck2);
    });

    // HeroPulse useEffect: deck_id changed (2 ≠ 1) → resets currentIndex to 0 → shows #1 of 1
    expect(await screen.findByText(/#1 of 1/)).toBeInTheDocument();
    // The new card title should be displayed
    expect(screen.getByText('Fresh Paper')).toBeInTheDocument();
  });

  it('clamps currentIndex when deck shrinks on refetch (same deck_id)', async () => {
    const user = userEvent.setup();

    // 5-card deck: allow the user to advance to index 3 (card #4)
    const fiveCards = Array.from({ length: 5 }, (_, i) =>
      makeCard({
        card_id: i + 1,
        paper_id: 100 + i,
        paper_title: `Paper ${i + 1}`,
        rank: i + 1,
      }),
    );
    const bigDeck = makeDeck({ deck_id: 10, card_count: 5, cards: fiveCards });

    // ratePulseCard resolves but invalidateQueries will re-fetch — keep returning bigDeck
    // until we swap it out after 3 ratings.
    vi.mocked(fetchPulseToday).mockResolvedValue(bigDeck);

    const { qc } = renderHeroPulse();

    // Wait for card #1 of 5
    expect(await screen.findByText(/#1 of 5/)).toBeInTheDocument();

    // Rate cards 1, 2, 3 (Skip each) to advance currentIndex to 3
    for (let i = 0; i < 3; i++) {
      // Prevent onSettled from invalidating/refetching mid-loop by keeping same deck
      const skipBtn = screen.getByRole('button', { name: 'Skip' });
      await user.click(skipBtn);
      // Wait for the index to advance before next click
      if (i < 2) {
        expect(await screen.findByText(new RegExp(`#${i + 2} of 5`))).toBeInTheDocument();
      }
    }

    // After 3 skips, currentIndex = 3 → "#4 of 5"
    expect(await screen.findByText(/#4 of 5/)).toBeInTheDocument();

    // Now simulate refetch returning same deck_id=10 but only 2 cards
    const card0 = fiveCards[0];
    const card1 = fiveCards[1];
    if (!card0 || !card1) throw new Error('fiveCards too short');
    const shrunkDeck = makeDeck({
      deck_id: 10,
      card_count: 2,
      cards: [card0, card1],
    });

    act(() => {
      qc.setQueryData<PulseDeck>(['pulse-today'], shrunkDeck);
    });

    // HeroPulse useEffect: deck_id unchanged (10 === 10), but currentIndex (3) >= cards.length (2)
    // → clamp fires → currentIndex = 2 → cleared state (currentIndex >= cards.length)
    expect(
      await screen.findByText(/All caught up/i),
    ).toBeInTheDocument();
  });

  it('clamp boundary (>=): refetch returning exact same length as currentIndex shows "All caught up"', async () => {
    const user = userEvent.setup();

    // 2-card deck
    const twoCards = [
      makeCard({ card_id: 1, paper_id: 101, paper_title: 'Card One', rank: 1 }),
      makeCard({ card_id: 2, paper_id: 102, paper_title: 'Card Two', rank: 2 }),
    ];
    const deck = makeDeck({ deck_id: 20, card_count: 2, cards: twoCards });
    vi.mocked(fetchPulseToday).mockResolvedValue(deck);

    const { qc } = renderHeroPulse();

    // Rate card 1 → currentIndex becomes 1, shows card 2
    expect(await screen.findByText('Card One')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Accept' }));
    expect(await screen.findByText('Card Two')).toBeInTheDocument();

    // Rate card 2 → currentIndex becomes 2 → 2 >= 2 → "All caught up"
    await user.click(screen.getByRole('button', { name: 'Accept' }));
    expect(await screen.findByText(/All caught up/i)).toBeInTheDocument();

    // Simulate refetch returning same deck (deck_id=20, still 2 cards)
    // currentIndex=2, deck.cards.length=2 → 2 >= 2 → clamp fires, stays cleared
    act(() => {
      qc.setQueryData<PulseDeck>(['pulse-today'], { ...deck });
    });

    // Should still show "All caught up" — the >= fix ensures clamp holds at the boundary
    expect(screen.getByText(/All caught up/i)).toBeInTheDocument();
  });
});

describe('HeroPulse empty-vs-error states (RED-ERROR-EMPTY-STATE)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a CALM empty-state CTA (not an error) when there is no deck (404 → null)', async () => {
    // fetchPulseToday maps the backend no-data 404 to `null`.
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
    renderHeroPulse();

    // Intentional invitation, not a broken/red error panel.
    expect(
      await screen.findByText(/No Pulse for today yet/i),
    ).toBeInTheDocument();
    const cta = screen.getByRole('link', { name: /Open Pulse Deck/i });
    expect(cta).toHaveAttribute('href', '/pulse');
    // The calm empty state must NOT surface the error sentinel.
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('renders the ErrorSentinel (role=status) on a GENUINE failure (5xx / network)', async () => {
    vi.mocked(fetchPulseToday).mockRejectedValue(new Error('boom'));
    renderHeroPulse();

    const sentinel = await screen.findByRole('status');
    expect(sentinel).toHaveTextContent(/Couldn't load your recommendations/i);
    // It must NOT show the no-data CTA.
    expect(screen.queryByRole('link', { name: /Open Pulse Deck/i })).toBeNull();
  });
});

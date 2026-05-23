import { useQuery } from '@tanstack/react-query';
import { fetchDecks, fetchCards, getStats } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';

/**
 * Fetch all decks.
 * Wraps the `['decks']` query key from the central registry.
 */
export function useDecks() {
  return useQuery({
    queryKey: QUERY_KEYS.decks.list(),
    queryFn: fetchDecks,
  });
}

/**
 * Fetch cards for a specific deck.
 * Wraps the `['cards', deckId]` query key from the central registry.
 */
export function useDeckCards(deckId: number) {
  return useQuery({
    queryKey: QUERY_KEYS.cards.byDeck(deckId),
    queryFn: () => fetchCards(deckId),
    enabled: deckId > 0,
  });
}

/**
 * Fetch retention stats (due_now, etc.).
 * Wraps the `['card-stats']` query key from the central registry.
 */
export function useCardStats() {
  return useQuery({
    queryKey: QUERY_KEYS.cards.stats(),
    queryFn: getStats,
    refetchInterval: 30_000,
  });
}

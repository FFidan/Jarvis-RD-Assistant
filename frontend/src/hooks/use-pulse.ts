import { useQuery } from '@tanstack/react-query';
import { fetchPulseToday, fetchPulseStats, fetchPulseDebug, explainPulseCard } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';

/**
 * Fetch today's Pulse deck.
 * Wraps the `['pulse-today']` query key from the central registry.
 */
export function usePulseToday() {
  return useQuery({
    queryKey: QUERY_KEYS.pulse.today(),
    queryFn: fetchPulseToday,
  });
}

/**
 * Fetch Pulse engagement stats.
 * Wraps the `['pulse-stats']` query key from the central registry.
 */
export function usePulseStats(days = 30) {
  return useQuery({
    queryKey: QUERY_KEYS.pulse.stats(days),
    queryFn: () => fetchPulseStats(days),
  });
}

/**
 * Fetch Pulse debug info.
 * Wraps the `['pulse-debug']` query key from the central registry.
 */
export function usePulseDebug() {
  return useQuery({
    queryKey: QUERY_KEYS.pulse.debug(),
    queryFn: fetchPulseDebug,
  });
}

/**
 * Fetch the "Why?" explanation for a single Pulse card.
 * Wraps the `['pulse-explain', cardId]` query key from the central registry.
 */
export function usePulseExplain(cardId: number) {
  return useQuery({
    queryKey: QUERY_KEYS.pulse.explain(cardId),
    queryFn: () => explainPulseCard(cardId),
    enabled: cardId > 0,
  });
}

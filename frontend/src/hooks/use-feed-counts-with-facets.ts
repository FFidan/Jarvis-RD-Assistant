import { useQuery } from '@tanstack/react-query';
import { fetchFeedCountsWithFacets } from '@/lib/api';
import type { FeedCountsWithFacets } from '@/types';

/**
 * UI_v3 §-facet rail — fetches the full counts payload including
 * `by_source`, `by_topic`, and `untagged` facet additions.
 *
 * The numeric-only `useFeedCounts` / `fetchFeedCounts` path is deliberately
 * kept separate so `CountsBadge` (which does `keyof FeedCountsResponse`
 * indexing) is not widened.
 */
export function useFeedCountsWithFacets() {
  return useQuery<FeedCountsWithFacets>({
    queryKey: ['feed-counts'],
    queryFn: fetchFeedCountsWithFacets,
    staleTime: 5_000,
  });
}

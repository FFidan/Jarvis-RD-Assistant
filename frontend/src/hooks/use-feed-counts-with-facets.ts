import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchFeedCountsWithFacets } from '@/lib/api';
import type { FeedCountsWithFacets } from '@/types';

/**
 * UI_v3 §-facet rail — fetches the full counts payload including
 * `by_source`, `by_topic`, and `untagged` facet additions.
 *
 * Pass `scope` to get scope-accurate facet counts from the backend
 * (C-FACET-BE). The query key includes scope so React Query re-fetches
 * when the user switches between My library / All discovered.
 *
 * The numeric-only `useFeedCounts` / `fetchFeedCounts` path is deliberately
 * kept separate so `CountsBadge` (which does `keyof FeedCountsResponse`
 * indexing) is not widened.
 */
export function useFeedCountsWithFacets(scope: 'library' | 'corpus' = 'library') {
  return useQuery<FeedCountsWithFacets>({
    queryKey: QUERY_KEYS.feed.counts(scope),
    queryFn: () => fetchFeedCountsWithFacets(scope),
    staleTime: 5_000,
  });
}

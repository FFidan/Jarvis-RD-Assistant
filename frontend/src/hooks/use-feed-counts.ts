import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchFeedCounts } from '@/lib/api';

export function useFeedCounts() {
  return useQuery({
    queryKey: QUERY_KEYS.feed.counts(),
    queryFn: fetchFeedCounts,
    staleTime: 5_000,
  });
}

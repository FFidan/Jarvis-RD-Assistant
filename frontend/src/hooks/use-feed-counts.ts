import { useQuery } from '@tanstack/react-query';
import { fetchFeedCounts } from '@/lib/api';

export function useFeedCounts() {
  return useQuery({
    queryKey: ['feed-counts'],
    queryFn: fetchFeedCounts,
    staleTime: 5_000,
  });
}

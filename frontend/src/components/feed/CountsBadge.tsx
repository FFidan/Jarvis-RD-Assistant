import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchFeedCounts } from '@/lib/api';
import type { FeedCountsResponse } from '@/types';

interface CountsBadgeProps {
  surface: keyof FeedCountsResponse;
}

export function CountsBadge({ surface }: CountsBadgeProps) {
  const { data, isLoading } = useQuery<FeedCountsResponse>({
    queryKey: QUERY_KEYS.feed.counts(),
    queryFn: () => fetchFeedCounts(),
    staleTime: 5_000,
  });
  if (isLoading || !data) return null;
  const count = data[surface];
  if (count === 0) return null;
  return (
    <span className="ml-2 inline-flex items-center justify-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground tabular-nums">
      {count > 999 ? '999+' : count}
    </span>
  );
}

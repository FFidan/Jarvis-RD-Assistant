import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import { RotateCcw, Loader2 } from 'lucide-react';
import { fetchRecommendationFeedback, deleteRecommendationFeedback } from '@/lib/api';
import type { FeedbackListItem } from '@/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';

interface TopicGroup {
  topic_id: number;
  topic_name: string;
  count: number;
}

function groupNegativeByTopic(items: FeedbackListItem[]): TopicGroup[] {
  const groups = new Map<number, TopicGroup>();
  for (const item of items) {
    if (item.signal !== 'negative') continue;
    if (item.topic_id == null) continue;
    const existing = groups.get(item.topic_id);
    if (existing) {
      existing.count += 1;
    } else {
      groups.set(item.topic_id, {
        topic_id: item.topic_id,
        topic_name: item.topic_name ?? `Topic ${item.topic_id}`,
        count: 1,
      });
    }
  }
  return Array.from(groups.values()).sort((a, b) => b.count - a.count);
}

export function RejectedTopicsPanel() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.topics.rejected(),
    queryFn: () => fetchRecommendationFeedback({ limit: 200 }),
    staleTime: 60_000,
  });

  const resetMutation = useMutation({
    mutationFn: (topicId: number) => deleteRecommendationFeedback(topicId),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.rejected() });
      toast.success(`Reset feedback for topic`, {
        description: `${res.deleted} feedback row(s) cleared.`,
      });
    },
    onError: (err) =>
      toast.error('Failed to reset topic feedback', {
        description: err instanceof Error ? err.message : 'Unknown error',
      }),
  });

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground py-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading rejected topics…
      </div>
    );
  }
  if (isError) {
    return (
      <div className="text-sm text-[var(--status-bad)] py-2">
        Failed to load rejected topics.
      </div>
    );
  }

  const groups = data ? groupNegativeByTopic(data.items) : [];

  if (groups.length === 0) {
    return (
      <div className="text-sm text-muted-foreground py-2">
        No topics rejected yet. Use the 👎 button on Pulse cards to dampen
        recommendations for topics you don&apos;t want.
      </div>
    );
  }

  return (
    <ul className="space-y-2">
      {groups.map((group) => (
        <li key={group.topic_id} className="flex items-center justify-between gap-3 rounded-md border p-2">
          <div className="flex items-center gap-2 min-w-0">
            <Badge variant="secondary" className="truncate">{group.topic_name}</Badge>
            <span className="text-xs text-muted-foreground whitespace-nowrap">
              {group.count} {group.count === 1 ? 'paper' : 'papers'} rejected
            </span>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={resetMutation.isPending}
            onClick={() => resetMutation.mutate(group.topic_id)}
            aria-label={`Reset feedback for ${group.topic_name}`}
          >
            <RotateCcw className="h-3.5 w-3.5 mr-1" />
            Reset
          </Button>
        </li>
      ))}
    </ul>
  );
}

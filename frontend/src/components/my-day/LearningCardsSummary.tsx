import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Brain, Flame } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { getStats } from '@/lib/api';
import type { RetentionStats } from '@/types';

function formatLastReview(iso: string | null | undefined): string {
  if (!iso) return 'Never';
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / (1000 * 60 * 60));
  if (h < 1) return 'Just now';
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export function LearningCardsSummary() {
  const { data, isLoading } = useQuery<RetentionStats>({
    queryKey: QUERY_KEYS.retention.stats(),
    queryFn: getStats,
    refetchInterval: 120_000,
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-lg">
            <Brain className="h-5 w-5" />
            Learning
          </CardTitle>
          {!isLoading && data && data.due_now > 0 && (
            <Button asChild size="sm" className="bg-orange-600 hover:bg-orange-700">
              <Link to="/cards">Review Now</Link>
            </Button>
          )}
        </div>
      </CardHeader>

      <CardContent>
        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-8 w-24" />
            <Skeleton className="h-4 w-32" />
          </div>
        ) : !data ? (
          <p className="text-sm text-muted-foreground text-center py-3">
            No learning data available.
          </p>
        ) : (
          <div className="space-y-3">
            {/* Cards due */}
            {data.due_now > 0 ? (
              <div className="flex items-center gap-3 rounded-lg bg-orange-50 dark:bg-orange-950/30 border border-orange-100 dark:border-orange-900 px-3 py-2.5">
                <p className="font-bold text-orange-800 dark:text-orange-300 text-xl tabular-nums">
                  {data.due_now}
                </p>
                <p className="text-xs text-orange-600 dark:text-orange-400 leading-tight">
                  cards due<br />Review to maintain streaks.
                </p>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No reviews pending.</p>
            )}

            {/* Stats row */}
            <div className="flex items-center gap-4 text-xs text-muted-foreground">
              <span className="flex items-center gap-1">
                <Flame className="h-3.5 w-3.5 text-orange-500" />
                {data.streak_days} day streak
              </span>
              <span>{data.reviewed_today} reviewed today</span>
              <span>
                Last: {formatLastReview(
                  // reviewed_today > 0 implies activity today — backend doesn't send
                  // last_review_at, so we approximate from reviewed_today
                  data.reviewed_today > 0 ? new Date().toISOString() : null,
                )}
              </span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

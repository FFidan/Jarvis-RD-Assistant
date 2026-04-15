import { useQuery } from '@tanstack/react-query';
import { BookOpen, Clock, Flame, Target, TrendingUp } from 'lucide-react';
import { getStats } from '@/lib/api';
import { MetricTile } from '@/components/MetricTile';
import { Skeleton } from '@/components/ui/skeleton';

export function StatsHeader() {
  const { data: stats, isLoading } = useQuery({
    queryKey: ['card-stats'],
    queryFn: getStats,
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {[1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-24" />)}
      </div>
    );
  }

  if (!stats) return null;

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      <MetricTile
        title="Total Cards"
        value={stats.total_cards}
        icon={BookOpen}
      />
      <MetricTile
        title="Due Now"
        value={stats.due_now}
        icon={Clock}
        subtitle={stats.due_now > 0 ? 'Ready for review' : 'All caught up'}
      />
      <MetricTile
        title="Reviewed Today"
        value={stats.reviewed_today}
        icon={TrendingUp}
      />
      <MetricTile
        title="Retention"
        value={`${stats.average_retention.toFixed(1)}%`}
        icon={Target}
        subtitle="Last 30 days"
        tooltip="Probability of correctly recalling a flashcard at review time, averaged across your deck. 0.9 = you recall 90% of cards when they come due."
      />
      <MetricTile
        title="Streak"
        value={`${stats.streak_days}d`}
        icon={Flame}
        subtitle={stats.streak_days > 0 ? 'Keep it up!' : 'Start reviewing'}
      />
    </div>
  );
}

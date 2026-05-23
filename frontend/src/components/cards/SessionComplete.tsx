/**
 * SessionComplete — panel shown when the review queue is exhausted for the session.
 *
 * Pulls fresh RetentionStats (already invalidated after each submitReview) and
 * displays streak, reviewed-today, retention. Offers a CTA to navigate to Library.
 */

import { useQuery } from '@tanstack/react-query';
import { CheckCircle, Flame, Target, TrendingUp } from 'lucide-react';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getStats } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';

interface SessionCompleteProps {
  sessionReviewed: number;
  onNavigateToLibrary: () => void;
}

export function SessionComplete({ sessionReviewed, onNavigateToLibrary }: SessionCompleteProps) {
  const { data: stats, isLoading } = useQuery({
    queryKey: QUERY_KEYS.cards.stats(),
    queryFn: getStats,
  });

  return (
    <div className="mx-auto max-w-xl py-16 flex flex-col items-center gap-6 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-full bg-[hsl(var(--ring)/0.12)]">
        <CheckCircle className="h-8 w-8 text-[hsl(var(--ring))]" />
      </div>

      <div className="space-y-1">
        <h2 className="text-2xl font-serif tracking-tight text-strong">Session complete</h2>
        {sessionReviewed > 0 && (
          <p className="text-sm text-muted-foreground">
            You reviewed {sessionReviewed} card{sessionReviewed !== 1 ? 's' : ''} this session.
          </p>
        )}
      </div>

      {isLoading ? (
        <div className="grid grid-cols-3 gap-4 w-full">
          {[1, 2, 3].map((i) => <Skeleton key={i} className="h-20 rounded-lg" />)}
        </div>
      ) : stats ? (
        <div className="grid grid-cols-3 gap-4 w-full">
          <StatTile
            icon={Flame}
            label="Streak"
            value={`${stats.streak_days}d`}
            iconClass="text-orange-500"
          />
          <StatTile
            icon={TrendingUp}
            label="Reviewed Today"
            value={String(stats.reviewed_today)}
            iconClass="text-blue-500"
          />
          <StatTile
            icon={Target}
            label="Retention"
            value={`${stats.average_retention.toFixed(1)}%`}
            iconClass="text-green-500"
          />
        </div>
      ) : null}

      <div className="flex flex-col gap-2 w-full max-w-xs">
        <Button onClick={onNavigateToLibrary} variant="outline" className="w-full">
          Manage library
        </Button>
      </div>
    </div>
  );
}

interface StatTileProps {
  icon: React.FC<{ className?: string }>;
  label: string;
  value: string;
  iconClass?: string;
}

function StatTile({ icon: Icon, label, value, iconClass }: StatTileProps) {
  return (
    <div className="flex flex-col items-center gap-1.5 rounded-lg border p-4">
      <Icon className={`h-5 w-5 ${iconClass ?? 'text-muted-foreground'}`} />
      <span className="text-xl font-semibold tabular-nums text-strong">{value}</span>
      <span className="text-[11px] text-muted-foreground">{label}</span>
    </div>
  );
}

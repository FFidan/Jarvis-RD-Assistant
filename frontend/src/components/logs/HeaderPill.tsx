import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { getSummary } from '@/lib/logs';
import { AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';

export function HeaderPill() {
  const navigate = useNavigate();

  const { data } = useQuery({
    queryKey: ['logs', 'summary', 'app-only'],
    // exclude_infra=1 so nginx rate-limit 503s (category=infra) don't inflate
    // the badge — those are self-inflicted infra noise, not application errors.
    queryFn: () => getSummary({ excludeInfra: true }),
    refetchInterval: 30_000,
    staleTime: 20_000,
  });

  // Count error + critical application events; infra events are excluded above.
  const errorCount = (data?.by_level?.error ?? 0) + (data?.by_level?.critical ?? 0);

  // Only show pill when there are errors
  if (errorCount === 0) return null;

  return (
    <button
      onClick={() => navigate('/logs?tab=events&level=error&since=24h')}
      title={`${errorCount} error${errorCount === 1 ? '' : 's'} in the last 24h — click to view`}
      className={cn(
        'flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-all',
        'bg-red-100 text-red-700 hover:bg-red-200',
        'dark:bg-red-900/30 dark:text-red-300 dark:hover:bg-red-900/50',
      )}
      aria-label={`${errorCount} recent errors`}
    >
      <AlertTriangle className="h-3.5 w-3.5" />
      <span>{errorCount}</span>
    </button>
  );
}

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getPulseSourceHealth, getPulseSourceHistory } from '@/lib/api';
import type { SourceHealth, SourceRunRecord } from '@/types/index';
import { ChevronDown, ChevronRight, CheckCircle, XCircle, Clock } from 'lucide-react';
import { cn } from '@/lib/utils';

function formatCooldown(until: string | null): string | null {
  if (!until) return null;
  const eta = new Date(until).getTime() - Date.now();
  if (eta <= 0) return null;
  const mins = Math.ceil(eta / 60_000);
  if (mins < 60) return `${mins}m`;
  return `${Math.round(mins / 60)}h ${mins % 60}m`;
}

function StatusIcon({ status }: { status: string | null }) {
  if (!status) return null;
  if (status === 'ok' || status === 'success')
    return <CheckCircle className="h-4 w-4 text-green-500" />;
  if (status === 'error' || status === 'failed')
    return <XCircle className="h-4 w-4 text-red-500" />;
  return <Clock className="h-4 w-4 text-yellow-500" />;
}

interface SourceRowProps {
  health: SourceHealth;
  history: SourceRunRecord[];
}

function SourceRow({ health, history }: SourceRowProps) {
  const [expanded, setExpanded] = useState(false);
  const cooldownEta = formatCooldown(health.cooldown_until);

  return (
    <div className="rounded-md border border-border overflow-hidden">
      <button
        className="w-full flex items-center gap-3 px-3 py-3 text-sm hover:bg-muted/50 transition-colors"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}

        <StatusIcon status={health.last_status} />

        <span className="font-medium">{health.source_type}</span>

        {cooldownEta && (
          <span className="ml-1 rounded px-1.5 py-0.5 text-xs bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300">
            cooldown: {cooldownEta}
          </span>
        )}

        {health.consecutive_failures > 0 && (
          <span className="rounded px-1.5 py-0.5 text-xs bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">
            {health.consecutive_failures} failures
          </span>
        )}

        <span className="ml-auto text-xs text-muted-foreground">
          {health.last_success_at
            ? `Last ok ${new Date(health.last_success_at).toLocaleDateString()}`
            : 'No success recorded'}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-border bg-muted/20 p-3">
          {history.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">No history available.</p>
          ) : (
            <SourceTimeline records={history} />
          )}
        </div>
      )}
    </div>
  );
}

function SourceTimeline({ records }: { records: SourceRunRecord[] }) {
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-muted-foreground mb-2">7-day timeline</div>
      <div className="flex gap-1 flex-wrap">
        {records.map((r, i) => (
          <div
            key={i}
            title={`${new Date(r.started_at).toLocaleString()} — ${r.status} (${r.candidate_count} candidates)`}
            className={cn(
              'h-5 w-5 rounded-sm text-[10px] flex items-center justify-center',
              r.status === 'ok' || r.status === 'success'
                ? 'bg-green-400/80'
                : r.status === 'error' || r.status === 'failed'
                  ? 'bg-red-400/80'
                  : 'bg-yellow-300/80',
            )}
          />
        ))}
      </div>
      <div className="text-xs text-muted-foreground mt-1 flex gap-3">
        <span>
          <span className="inline-block h-2 w-2 rounded-sm bg-green-400 mr-1" />
          ok
        </span>
        <span>
          <span className="inline-block h-2 w-2 rounded-sm bg-red-400 mr-1" />
          error
        </span>
        <span>
          <span className="inline-block h-2 w-2 rounded-sm bg-yellow-300 mr-1" />
          other
        </span>
      </div>
    </div>
  );
}

export function SourcesTab() {
  const { data: healthData, isLoading: healthLoading } = useQuery({
    queryKey: ['pulse', 'source-health'],
    queryFn: getPulseSourceHealth,
    refetchInterval: 30_000,
  });

  const { data: historyData, isLoading: historyLoading } = useQuery({
    queryKey: ['pulse', 'source-history', 7],
    queryFn: () => getPulseSourceHistory(7),
    refetchInterval: 60_000,
  });

  const isLoading = healthLoading || historyLoading;
  const sources = healthData ?? [];

  return (
    <div className="space-y-3">
      {isLoading && (
        <p className="text-sm text-muted-foreground">Loading source health…</p>
      )}
      {!isLoading && sources.length === 0 && (
        <p className="text-sm text-muted-foreground">No source data available.</p>
      )}
      {sources.map((h) => (
        <SourceRow
          key={h.source_type}
          health={h}
          history={historyData?.[h.source_type] ?? []}
        />
      ))}
    </div>
  );
}

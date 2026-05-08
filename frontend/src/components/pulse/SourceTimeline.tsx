import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import type { SourceRunRecord } from '@/types';

interface Props {
  sourceType: string;
  runs: SourceRunRecord[];
}

const STATUS_COLORS: Record<string, string> = {
  ok: 'bg-green-500',
  rate_limit: 'bg-yellow-400',
  error: 'bg-red-500',
  cooldown_skip: 'bg-gray-400',
};

function statusColor(status: string): string {
  return STATUS_COLORS[status] ?? 'bg-gray-300';
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

export function SourceTimeline({ sourceType, runs }: Props) {
  if (runs.length === 0) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <span className="w-28 shrink-0 truncate font-medium">{sourceType}</span>
        <span>No runs in window</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2" data-testid={`source-timeline-${sourceType}`}>
      <span className="w-28 shrink-0 truncate text-xs font-medium">{sourceType}</span>
      <TooltipProvider delayDuration={100}>
        <div className="flex flex-wrap items-center gap-1">
          {runs.map((run) => (
            <Tooltip key={run.started_at}>
              <TooltipTrigger asChild>
                <span
                  className={`inline-block h-3 w-3 rounded-full cursor-default ${statusColor(run.status)}`}
                  aria-label={`${run.status} at ${run.started_at}`}
                  data-testid={`run-dot-${run.status}`}
                />
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                <p className="font-medium">{run.status}</p>
                <p>{formatTimestamp(run.started_at)}</p>
                <p>Candidates: {run.candidate_count}</p>
                {run.duration_ms != null && (
                  <p>Duration: {(run.duration_ms / 1000).toFixed(1)}s</p>
                )}
              </TooltipContent>
            </Tooltip>
          ))}
        </div>
      </TooltipProvider>
    </div>
  );
}

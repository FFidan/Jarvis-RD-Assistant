import { useState, useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { listJobs } from '@/lib/api';
import { listEvents, streamCorrelation } from '@/lib/logs';
import type { SystemEvent } from '@/lib/logs';
import type { Job } from '@/stores/job-store';
import { LEVEL_BADGE_CLASSES, CATEGORY_BADGE_CLASSES } from './utils';
import { ChevronDown, ChevronRight, Wifi, WifiOff } from 'lucide-react';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Running-job row with collapsible SSE stream
// ---------------------------------------------------------------------------

interface JobStreamRowProps {
  job: Job;
}

function JobStreamRow({ job }: JobStreamRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [streamEvents, setStreamEvents] = useState<SystemEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const closeRef = useRef<(() => void) | null>(null);

  // Derive correlation_id from job: try context field first, else use job id
  const correlationId: string | null =
    (job.payload?.correlation_id as string | undefined) ?? null;

  useEffect(() => {
    if (!expanded || !correlationId) return;
    setStreaming(true);
    const handle = streamCorrelation(correlationId, {
      onEvent: (ev) => setStreamEvents((prev) => [...prev, ev]),
      onDone: () => setStreaming(false),
    });
    closeRef.current = handle.close;
    return () => {
      handle.close();
      closeRef.current = null;
    };
  }, [expanded, correlationId]);

  return (
    <div className="rounded-md border border-border overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-muted/50 transition-colors"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}
        <span
          className={cn(
            'inline-block h-2 w-2 rounded-full',
            job.status === 'running'
              ? 'bg-green-500 animate-pulse'
              : 'bg-muted-foreground',
          )}
        />
        <span className="font-medium truncate">{job.kind}</span>
        <span className="ml-auto text-xs text-muted-foreground">{job.status}</span>
        {correlationId && (
          <span className="text-xs text-muted-foreground font-mono truncate max-w-[8rem]">
            {correlationId}
          </span>
        )}
      </button>

      {expanded && (
        <div className="border-t border-border bg-muted/20 p-3 space-y-1 max-h-64 overflow-y-auto">
          {!correlationId && (
            <p className="text-xs text-muted-foreground italic">
              No correlation ID available for this job.
            </p>
          )}
          {correlationId && streaming && streamEvents.length === 0 && (
            <p className="text-xs text-muted-foreground flex items-center gap-1">
              <Wifi className="h-3 w-3 animate-pulse" /> Connecting…
            </p>
          )}
          {streamEvents.map((ev, i) => (
            <div key={`${ev.id}-${i}`} className="flex items-start gap-2 text-xs">
              <span className="text-muted-foreground shrink-0 font-mono">
                {new Date(ev.created_at).toLocaleTimeString()}
              </span>
              <span
                className={cn(
                  'shrink-0 rounded px-1 font-medium',
                  LEVEL_BADGE_CLASSES[ev.level] ?? '',
                )}
              >
                {ev.level}
              </span>
              <span className="break-all">{ev.message}</span>
            </div>
          ))}
          {!streaming && streamEvents.length > 0 && (
            <div className="flex items-center gap-1 text-xs text-muted-foreground pt-1">
              <WifiOff className="h-3 w-3" /> Stream ended
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// LiveTab
// ---------------------------------------------------------------------------

export function LiveTab() {
  const { data: jobs } = useQuery({
    queryKey: ['jobs', 'running'],
    queryFn: () => listJobs({ status: 'running' }),
    refetchInterval: 3_000,
  });

  const { data: recentData } = useQuery({
    queryKey: ['logs', 'recent'],
    queryFn: () => listEvents({ limit: 50 }),
    refetchInterval: 3_000,
  });

  const runningJobs = jobs ?? [];
  const recentEvents = recentData?.events ?? [];

  return (
    <div className="space-y-6">
      {/* Running jobs */}
      <section>
        <h3 className="text-sm font-semibold mb-3 text-foreground">
          Running Jobs{' '}
          {runningJobs.length > 0 && (
            <span className="ml-1 text-xs text-muted-foreground">
              ({runningJobs.length})
            </span>
          )}
        </h3>
        {runningJobs.length === 0 ? (
          <p className="text-sm text-muted-foreground">No jobs currently running.</p>
        ) : (
          <div className="space-y-2">
            {runningJobs.map((job) => (
              <JobStreamRow key={job.id} job={job} />
            ))}
          </div>
        )}
      </section>

      {/* Recent events */}
      <section>
        <h3 className="text-sm font-semibold mb-3 text-foreground">
          Recent Events (last 50)
        </h3>
        {recentEvents.length === 0 ? (
          <p className="text-sm text-muted-foreground">No recent events.</p>
        ) : (
          <div className="rounded-md border border-border overflow-hidden divide-y divide-border">
            {recentEvents.map((ev) => (
              <div
                key={ev.id}
                className="flex items-start gap-3 px-3 py-2 text-xs hover:bg-muted/30"
              >
                <span className="text-muted-foreground shrink-0 font-mono whitespace-nowrap">
                  {new Date(ev.created_at).toLocaleTimeString()}
                </span>
                <span
                  className={cn(
                    'shrink-0 rounded px-1 py-0.5 font-medium',
                    LEVEL_BADGE_CLASSES[ev.level] ?? '',
                  )}
                >
                  {ev.level}
                </span>
                <span
                  className={cn(
                    'shrink-0 rounded px-1 py-0.5',
                    CATEGORY_BADGE_CLASSES[ev.category] ?? '',
                  )}
                >
                  {ev.category}
                </span>
                <span className="text-muted-foreground shrink-0">{ev.source}</span>
                <span className="break-all flex-1">{ev.message}</span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

/**
 * TopBar jobs indicator — shows a live count of queued/running jobs and a
 * popover listing each job's progress. Hidden when there are no active jobs.
 */

import { Activity, X } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Progress } from '@/components/ui/progress';
import { Button } from '@/components/ui/button';
import { useJobStore, type Job } from '@/stores/job-store';
import { kindLabel } from '@/lib/labels/jobKinds';
import { jobOutcomeCounts } from '@/lib/job-outcome';

type DisplayStatus = Job['status'] | 'partial';

function statusColor(status: DisplayStatus): string {
  switch (status) {
    case 'queued': return 'text-muted-foreground';
    case 'running': return 'text-blue-500';
    case 'succeeded': return 'text-green-500';
    case 'partial': return 'text-[var(--status-warn)]';
    case 'failed': return 'text-destructive';
    case 'cancelled': return 'text-muted-foreground';
    default: return 'text-muted-foreground';
  }
}

function statusLabel(status: DisplayStatus): string {
  switch (status) {
    case 'queued': return 'Queued';
    case 'running': return 'Running';
    case 'succeeded': return 'Done';
    case 'partial': return 'Partial';
    case 'failed': return 'Failed';
    case 'cancelled': return 'Cancelled';
    default: return status;
  }
}

/**
 * A job that reached `succeeded` but reports a `partial` or `cancelled` result
 * (e.g. `papers.process_library`) left work undone. Returns a short
 * "N failed, M skipped, R not processed of T" line, or null for a plain
 * success. A partial result without countable outcomes gets a neutral fallback
 * instead of a misleading all-zero summary.
 */
function outcomeSummary(job: Job): string | null {
  const r = (job.result ?? {}) as {
    status?: string;
  };
  if (job.status !== 'succeeded') return null;
  if (r.status !== 'partial' && r.status !== 'cancelled') return null;
  const { failed, skipped, remaining, total } = jobOutcomeCounts(job.result);
  if (failed === 0 && skipped === 0 && remaining === 0) return 'Details unavailable';
  const remainingSummary = remaining > 0 ? `, ${remaining} not processed` : '';
  return `${failed} failed, ${skipped} skipped${remainingSummary} of ${total}`;
}

/**
 * Some handlers return a terminal outcome instead of raising. Keep the API job
 * status unchanged while showing users whether work completed only partially
 * or stopped through cancellation.
 */
function effectiveStatus(job: Job): DisplayStatus {
  const resultStatus = (job.result as { status?: string } | null)?.status;
  if (job.status !== 'succeeded') return job.status;
  if (resultStatus === 'partial' || resultStatus === 'cancelled') return resultStatus;
  return job.status;
}

interface JobRowProps {
  job: Job;
  onCancel: (id: string) => void;
  onRemove: (id: string) => void;
}

function JobRow({ job, onCancel, onRemove }: JobRowProps) {
  const isActive = job.status === 'queued' || job.status === 'running';
  const isTerminal = job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled';
  const summary = outcomeSummary(job);
  const shownStatus = effectiveStatus(job);
  // A cancel REQUEST does not move `status` — the handler keeps running until it
  // observes the flag — so an in-flight cancel is its own display state: neither
  // the `running` it still technically is, nor the `cancelled` it has not
  // reached. Without it the row would look untouched after the click.
  const isCancelling = job.cancel_requested === true && !isTerminal;

  return (
    <div className="flex flex-col gap-1 py-2 border-b last:border-b-0">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium truncate">
          {kindLabel(job.kind, { paperScoped: job.payload?.paper_id != null })}
        </span>
        <div className="flex items-center gap-1 shrink-0">
          {isCancelling ? (
            <span className="text-xs text-[var(--status-warn)]">Cancelling…</span>
          ) : (
            <span className={`text-xs ${statusColor(shownStatus)}`}>{statusLabel(shownStatus)}</span>
          )}
          {isActive && (
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5"
              onClick={() => onCancel(job.id)}
              disabled={isCancelling}
              title={isCancelling ? 'Cancellation requested — finishing current step' : 'Cancel job'}
            >
              <X className="h-3 w-3" />
            </Button>
          )}
          {isTerminal && (
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5"
              onClick={() => onRemove(job.id)}
              title="Dismiss"
            >
              <X className="h-3 w-3" />
            </Button>
          )}
        </div>
      </div>

      {isActive && (
        <Progress value={(job.progress ?? 0) * 100} className="h-1.5" />
      )}

      {job.progress_message && (
        <p className="text-xs text-muted-foreground truncate">{job.progress_message}</p>
      )}

      {job.status === 'failed' && job.error && (
        <p className="text-xs text-destructive truncate">{job.error.message}</p>
      )}

      {summary && (
        <p
          role="status"
          aria-label={`Incomplete: ${summary}`}
          className="text-xs text-[var(--status-warn)] truncate"
        >
          {summary}
        </p>
      )}
    </div>
  );
}

export function JobsIndicator() {
  const jobs = useJobStore((s) => s.jobs);
  const cancelJob = useJobStore((s) => s.cancelJob);
  const removeJob = useJobStore((s) => s.removeJob);

  const allJobs = Object.values(jobs);
  const activeJobs = allJobs.filter(
    (j) => j.status === 'running' || j.status === 'queued',
  );
  const recentTerminal = allJobs.filter(
    (j) => j.status === 'succeeded' || j.status === 'failed' || j.status === 'cancelled',
  );

  const runningCount = activeJobs.length;

  // Hide entirely when nothing to show
  if (allJobs.length === 0) return null;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="relative h-8 w-8"
          title={`${runningCount} job${runningCount !== 1 ? 's' : ''} running`}
          aria-label="Background tasks"
        >
          <Activity className="h-4 w-4" />
          {runningCount > 0 && (
            <span className="absolute -top-0.5 -right-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-blue-500 text-[10px] font-bold text-white leading-none">
              {runningCount > 9 ? '9+' : runningCount}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        className="w-80 p-3"
        align="end"
        sideOffset={8}
      >
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-semibold">Background tasks</span>
          {runningCount > 0 && (
            <span className="text-xs text-muted-foreground">
              {runningCount} running
            </span>
          )}
        </div>

        {activeJobs.length > 0 && (
          <div className="mb-1">
            {activeJobs.map((job) => (
              <JobRow key={job.id} job={job} onCancel={cancelJob} onRemove={removeJob} />
            ))}
          </div>
        )}

        {recentTerminal.length > 0 && (
          <>
            {activeJobs.length > 0 && (
              <div className="my-2 border-t" />
            )}
            <p className="mb-1 text-xs text-muted-foreground">Recent</p>
            {recentTerminal.map((job) => (
              <JobRow key={job.id} job={job} onCancel={cancelJob} onRemove={removeJob} />
            ))}
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}

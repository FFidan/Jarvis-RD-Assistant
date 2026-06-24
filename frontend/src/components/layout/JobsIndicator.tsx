/**
 * TopBar jobs indicator — shows a live count of queued/running jobs and a
 * popover listing each job's progress. Hidden when there are no active jobs.
 */

import { Activity, X } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Progress } from '@/components/ui/progress';
import { Button } from '@/components/ui/button';
import { useJobStore, type Job } from '@/stores/job-store';

const KIND_LABELS: Record<string, string> = {
  'pulse.generate': 'Generating Pulse',
  'pulse.train_classifier': 'Training Pulse',
  'paper.process': 'Processing PDF',
  'paper.analyze': 'Analyzing Paper',
  'paper.download': 'Downloading PDF',
  'paper.summarize': 'Summarizing',
  'papers.batch_summarize': 'Batch Summarize',
  'papers.batch_process': 'Batch Process',
  'papers.scan_local': 'Scanning Local PDFs',
  'extraction.single': 'Extracting',
  'extraction.batch': 'Batch Extraction',
  'citations.batch_fetch': 'Fetching Citations',
  'contradictions.scan': 'Scanning Contradictions',
  'digest.weekly': 'Weekly Digest',
  'card.generate': 'Generating Cards',
  'card.generate_batch': 'Batch Card Generation',
  'zotero.push': 'Pushing to Zotero',
  'zotero.resync': 'Resyncing Zotero',
  'zotero.sync_from_zotero': 'Syncing Zotero',
  'zotero.sync_annotations': 'Syncing Highlights',
};

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

function statusColor(status: Job['status']): string {
  switch (status) {
    case 'queued': return 'text-muted-foreground';
    case 'running': return 'text-blue-500';
    case 'succeeded': return 'text-green-500';
    case 'failed': return 'text-destructive';
    case 'cancelled': return 'text-muted-foreground';
    default: return 'text-muted-foreground';
  }
}

function statusLabel(status: Job['status']): string {
  switch (status) {
    case 'queued': return 'Queued';
    case 'running': return 'Running';
    case 'succeeded': return 'Done';
    case 'failed': return 'Failed';
    case 'cancelled': return 'Cancelled';
    default: return status;
  }
}

interface JobRowProps {
  job: Job;
  onCancel: (id: string) => void;
  onRemove: (id: string) => void;
}

function JobRow({ job, onCancel, onRemove }: JobRowProps) {
  const isActive = job.status === 'queued' || job.status === 'running';
  const isTerminal = job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled';

  return (
    <div className="flex flex-col gap-1 py-2 border-b last:border-b-0">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium truncate">{kindLabel(job.kind)}</span>
        <div className="flex items-center gap-1 shrink-0">
          <span className={`text-xs ${statusColor(job.status)}`}>{statusLabel(job.status)}</span>
          {isActive && (
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5"
              onClick={() => onCancel(job.id)}
              title="Cancel job"
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

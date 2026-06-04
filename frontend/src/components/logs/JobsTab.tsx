import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { listJobs } from '@/lib/api';
import type { Job } from '@/stores/job-store';
import { CorrelationDrawer } from './CorrelationDrawer';
import { cn } from '@/lib/utils';

const ALL_STATUSES = ['running', 'succeeded', 'failed', 'cancelled'] as const;
type StatusFilter = typeof ALL_STATUSES[number] | 'all';

const STATUS_CHIP_CLASSES: Record<string, string> = {
  running: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  succeeded: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  cancelled: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
};

function formatDuration(job: Job): string {
  if (!job.started_at) return '—';
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const ms = end.getTime() - new Date(job.started_at).getTime();
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

export function JobsTab() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [drawerCorrelationId, setDrawerCorrelationId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const { data: jobs, isLoading } = useQuery({
    queryKey: QUERY_KEYS.jobs.list(statusFilter === 'all' ? undefined : statusFilter),
    queryFn: () =>
      listJobs(statusFilter === 'all' ? undefined : { status: statusFilter }),
    refetchInterval: 5_000,
  });

  function openDrawer(job: Job) {
    const corrId = (job.payload?.correlation_id as string | undefined) ?? job.id;
    setDrawerCorrelationId(corrId);
    setDrawerOpen(true);
  }

  return (
    <div className="space-y-4">
      {/* Status filter chips */}
      <div className="flex flex-wrap gap-2">
        <FilterChip
          label="All"
          active={statusFilter === 'all'}
          onClick={() => setStatusFilter('all')}
          className="bg-muted text-foreground"
        />
        {ALL_STATUSES.map((s) => (
          <FilterChip
            key={s}
            label={s}
            active={statusFilter === s}
            onClick={() => setStatusFilter(s)}
            className={STATUS_CHIP_CLASSES[s] ?? ''}
          />
        ))}
      </div>

      {/* Jobs table */}
      {isLoading && (
        <p className="text-sm text-muted-foreground">Loading jobs…</p>
      )}
      {!isLoading && (!jobs || jobs.length === 0) && (
        <p className="text-sm text-muted-foreground">No jobs found.</p>
      )}
      {jobs && jobs.length > 0 && (
        <div className="rounded-md border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted text-muted-foreground text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Kind</th>
                <th className="px-3 py-2 text-left font-medium">Status</th>
                <th className="px-3 py-2 text-left font-medium">Duration</th>
                <th className="px-3 py-2 text-left font-medium">Started</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {jobs.map((job) => (
                <tr
                  key={job.id}
                  className="hover:bg-muted/40 cursor-pointer"
                  onClick={() => openDrawer(job)}
                >
                  <td className="px-3 py-2 font-medium truncate max-w-[12rem]">
                    {job.kind}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-xs font-medium',
                        STATUS_CHIP_CLASSES[job.status] ?? '',
                      )}
                    >
                      {job.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {formatDuration(job)}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
                    {job.started_at
                      ? new Date(job.started_at).toLocaleString()
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <CorrelationDrawer
        correlationId={drawerCorrelationId}
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
      />
    </div>
  );
}

interface FilterChipProps {
  label: string;
  active: boolean;
  onClick: () => void;
  className?: string;
}

function FilterChip({ label, active, onClick, className }: FilterChipProps) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'rounded-full px-3 py-1 text-xs font-medium transition-all border',
        active
          ? cn(className, 'border-foreground/30 ring-1 ring-foreground/30')
          : cn(className, 'border-transparent opacity-60 hover:opacity-100'),
      )}
    >
      {label}
    </button>
  );
}

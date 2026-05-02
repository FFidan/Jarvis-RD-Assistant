import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, InboxIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useJobStore } from '@/stores/job-store';
import { fetchFeedPapers } from '@/lib/api';
import type { FeedPaper } from '@/types';

const COLLAPSE_THRESHOLD = 5;

export function ActionItemsCard() {
  const queryClient = useQueryClient();
  const startJob = useJobStore((s) => s.startJob);
  const isRunning = useJobStore((s) => s.isRunning);

  /** Papers saved but not yet processed (no chunks). */
  const {
    data: savedFeed,
    isLoading,
    isError,
    refetch,
  } = useQuery({
    queryKey: ['action-items-unprocessed'],
    queryFn: () => fetchFeedPapers({ statuses: 'new', limit: 10 }),
    refetchInterval: 60_000,
  });

  /** Failed jobs from the job store (paper.process kind). */
  const jobs = useJobStore((s) => s.jobs);
  const failedJobs = useMemo(
    () =>
      Object.values(jobs).filter(
        (j) => j.kind === 'paper.process' && j.status === 'failed',
      ),
    [jobs],
  );

  const unprocessed: FeedPaper[] = savedFeed?.papers ?? [];

  // Accordion: start open; auto-collapse once data loads and list is long (>5).
  // After the user manually toggles, don't auto-reset on re-fetches.
  const [isOpen, setIsOpen] = useState(true);
  const [userToggled, setUserToggled] = useState(false);

  useEffect(() => {
    if (!userToggled && savedFeed !== undefined) {
      const total = (savedFeed.papers?.length ?? 0);
      setIsOpen(total <= COLLAPSE_THRESHOLD);
    }
  }, [savedFeed, userToggled]);

  const handleProcessAll = useCallback(async () => {
    await Promise.all(
      unprocessed
        .filter((p) => p.pdf_downloaded && !isRunning('paper.process', { paper_id: p.id }))
        .map((p) => startJob('paper.process', { paper_id: p.id }).catch(() => {})),
    );
    // Refresh after queuing
    queryClient.invalidateQueries({ queryKey: ['action-items-unprocessed'] });
  }, [unprocessed, startJob, isRunning, queryClient]);

  const processable = unprocessed.filter((p) => p.pdf_downloaded);

  if (isLoading) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <Skeleton className="h-5 w-40" />
        </CardHeader>
        <CardContent className="space-y-2">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-lg">
            <InboxIcon className="h-5 w-5" />
            Action Items
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
            <span>
              Could not load action items.{' '}
              <button onClick={() => refetch()} className="underline font-medium">
                Retry
              </button>
            </span>
          </div>
        </CardContent>
      </Card>
    );
  }

  const isEmpty = !isLoading && !isError && unprocessed.length === 0 && failedJobs.length === 0;
  const totalItems = unprocessed.length + failedJobs.length;
  const showToggle = totalItems > COLLAPSE_THRESHOLD;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-lg">
            <InboxIcon className="h-5 w-5" />
            Action Items
            {totalItems > 0 && (
              <span className="text-sm font-normal text-muted-foreground">
                ({totalItems})
              </span>
            )}
          </CardTitle>
          <div className="flex items-center gap-2">
            {processable.length > 0 && (
              <Button size="sm" variant="outline" onClick={handleProcessAll}
                title="Process the papers whose PDF is already downloaded">
                Process all ({processable.length})
              </Button>
            )}
            {showToggle && (
              <Button
                size="sm"
                variant="ghost"
                className="h-7 px-2 text-xs text-muted-foreground"
                onClick={() => { setUserToggled(true); setIsOpen((v) => !v); }}
                aria-expanded={isOpen}
              >
                {isOpen ? (
                  <>
                    <ChevronUp className="h-3.5 w-3.5 mr-1" />
                    Collapse
                  </>
                ) : (
                  <>
                    <ChevronDown className="h-3.5 w-3.5 mr-1" />
                    Expand to triage
                  </>
                )}
              </Button>
            )}
          </div>
        </div>
        <p className="text-xs text-muted-foreground mt-1">
          Papers discovered by your sources that haven&apos;t been indexed for AI search yet
        </p>
      </CardHeader>

      <CardContent>
        {isEmpty ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <CheckCircle2 className="h-4 w-4 text-green-500 shrink-0" />
            <span>You&apos;re all caught up</span>
          </div>
        ) : !isOpen && showToggle ? (
          /* Collapsed: count summary only — header carries the toggle. */
          <p className="py-2 text-sm text-muted-foreground">
            {unprocessed.length} paper{unprocessed.length !== 1 ? 's' : ''} need
            {unprocessed.length === 1 ? 's' : ''} processing
            {failedJobs.length > 0 && `, ${failedJobs.length} failed`}
          </p>
        ) : (
          <div className="space-y-2">
            {/* Failed jobs */}
            {failedJobs.map((job) => (
              <div
                key={job.id}
                className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-sm"
              >
                <AlertCircle className="h-4 w-4 mt-0.5 shrink-0 text-destructive" />
                <div className="min-w-0">
                  <p className="font-medium text-destructive">Processing failed</p>
                  <p className="text-xs text-muted-foreground truncate">
                    {job.error?.message ?? 'Unknown error'}
                  </p>
                </div>
              </div>
            ))}

            {/* Unprocessed papers */}
            {unprocessed.map((paper) => (
              <div
                key={paper.id}
                className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2.5 text-sm"
              >
                <div className="min-w-0 flex-1">
                  <Link
                    to={`/paper/${paper.id}`}
                    className="font-medium leading-tight line-clamp-1 hover:underline"
                    title={paper.title}
                  >
                    {paper.title}
                  </Link>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {paper.pdf_downloaded ? 'PDF ready — needs processing' : 'No PDF yet'}
                  </p>
                </div>
                <Link
                  to={`/paper/${paper.id}?action=process`}
                  className="shrink-0"
                >
                  <Button size="sm" variant="secondary" className="h-7 text-xs">
                    Process
                  </Button>
                </Link>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

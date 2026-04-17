import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, InboxIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useJobStore } from '@/stores/job-store';
import { fetchFeedPapers } from '@/lib/api';
import type { FeedPaper } from '@/types';

export function ActionItemsCard() {
  const queryClient = useQueryClient();
  const startJob = useJobStore((s) => s.startJob);

  /** Papers saved but not yet processed (no chunks). */
  const {
    data: savedFeed,
    isLoading,
  } = useQuery({
    queryKey: ['action-items-unprocessed'],
    queryFn: () => fetchFeedPapers({ statuses: 'new', limit: 10 }),
    refetchInterval: 60_000,
  });

  /** Failed jobs from the job store (paper.process kind). */
  const failedJobs = useJobStore((s) =>
    Object.values(s.jobs).filter(
      (j) => j.kind === 'paper.process' && j.status === 'failed',
    ),
  );

  const unprocessed: FeedPaper[] = savedFeed?.papers ?? [];

  const handleProcessAll = useCallback(async () => {
    for (const paper of unprocessed) {
      if (!paper.pdf_downloaded) continue; // can only process if PDF exists
      await startJob('paper.process', { paper_id: paper.id }).catch(() => {});
    }
    // Refresh after queuing
    queryClient.invalidateQueries({ queryKey: ['action-items-unprocessed'] });
  }, [unprocessed, startJob, queryClient]);

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

  const isEmpty = unprocessed.length === 0 && failedJobs.length === 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-lg">
            <InboxIcon className="h-5 w-5" />
            Action Items
          </CardTitle>
          {processable.length > 0 && (
            <Button size="sm" variant="outline" onClick={handleProcessAll}>
              Process all ({processable.length})
            </Button>
          )}
        </div>
      </CardHeader>

      <CardContent>
        {isEmpty ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <CheckCircle2 className="h-4 w-4 text-green-500 shrink-0" />
            <span>You&apos;re all caught up</span>
          </div>
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

            {unprocessed.length === 0 && failedJobs.length > 0 && null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

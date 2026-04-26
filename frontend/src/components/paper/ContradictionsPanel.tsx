import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangle, Loader2, ScanSearch } from 'lucide-react';
import { fetchContradictions, scanPaperContradictions } from '@/lib/api';
import { useJobStore, type Job } from '@/stores/job-store';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

interface ContradictionsPanelProps {
  paperId: number;
}

const CONTRADICTION_LIMIT = 20;

function pageLabel(page: number | null): string {
  return page == null ? 'page unknown' : `p. ${page}`;
}

function confidenceLabel(confidence: number): string {
  const percent = confidence <= 1 ? confidence * 100 : confidence;
  return `${Math.round(percent)}%`;
}

function jobStatus(status: string): Job['status'] {
  if (status === 'running' || status === 'succeeded' || status === 'failed' || status === 'cancelled') {
    return status;
  }
  return 'queued';
}

export function ContradictionsPanel({ paperId }: ContradictionsPanelProps) {
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);
  const isScanning = useJobStore((s) =>
    s.isRunning('contradictions.scan', { paper_id: paperId }),
  );

  const {
    data,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ['contradictions', paperId, 'verified'],
    queryFn: () => fetchContradictions({ paper_id: paperId, status: 'verified', limit: CONTRADICTION_LIMIT }),
    enabled: Number.isFinite(paperId) && paperId > 0,
  });

  const scanMutation = useMutation({
    mutationFn: () => scanPaperContradictions(paperId, { limit: CONTRADICTION_LIMIT }),
    onSuccess: (result) => {
      trackExternalJob({
        jobId: result.job_id,
        kind: 'contradictions.scan',
        payload: { paper_id: paperId, limit: CONTRADICTION_LIMIT },
        status: jobStatus(result.status),
      });
    },
  });

  const contradictions = data?.contradictions ?? [];
  const pending = scanMutation.isPending || isScanning;

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <AlertTriangle className="h-4 w-4 text-amber-500" />
          Contradictions
        </h3>
        {data && (
          <Badge variant={contradictions.length > 0 ? 'destructive' : 'secondary'}>
            {data.total}
          </Badge>
        )}
      </div>

      <Button
        size="sm"
        variant="outline"
        className="w-full justify-start"
        onClick={() => scanMutation.mutate()}
        disabled={pending}
      >
        {pending ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <ScanSearch className="mr-2 h-4 w-4" />
        )}
        {pending ? 'Scanning...' : 'Scan contradictions'}
      </Button>

      {scanMutation.isError && (
        <p className="text-xs text-destructive">
          {scanMutation.error instanceof Error
            ? scanMutation.error.message
            : 'Failed to queue contradiction scan.'}
        </p>
      )}

      {isLoading ? (
        <p className="text-xs text-muted-foreground">Loading contradictions...</p>
      ) : isError ? (
        <p className="text-xs text-destructive">Failed to load contradictions.</p>
      ) : contradictions.length === 0 ? (
        <p className="text-xs text-muted-foreground">No verified contradictions found.</p>
      ) : (
        <div className="space-y-3">
          {contradictions.map((item) => {
            const currentIsA = item.paper_a_id === paperId;
            const otherTitle = currentIsA ? item.paper_b_title : item.paper_a_title;
            const currentFinding = currentIsA ? item.finding_a : item.finding_b;
            const otherFinding = currentIsA ? item.finding_b : item.finding_a;
            const currentQuote = currentIsA ? item.quote_a : item.quote_b;
            const otherQuote = currentIsA ? item.quote_b : item.quote_a;
            const currentPage = currentIsA ? item.page_a : item.page_b;
            const otherPage = currentIsA ? item.page_b : item.page_a;

            return (
              <article key={item.id} className="space-y-2 rounded-md border p-3 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <Badge variant="outline" className="truncate">
                    {item.contradiction_type}
                  </Badge>
                  <span className="shrink-0 text-muted-foreground">
                    {confidenceLabel(item.confidence)}
                  </span>
                </div>
                <p className="font-medium leading-snug">{otherTitle}</p>
                <p className="text-muted-foreground">{item.explanation}</p>
                <div className="space-y-1">
                  <p>
                    <span className="font-medium">This paper:</span> {currentFinding}
                  </p>
                  <blockquote className="border-l-2 border-amber-500/50 pl-2 text-muted-foreground">
                    &ldquo;{currentQuote}&rdquo; <span>({pageLabel(currentPage)})</span>
                  </blockquote>
                </div>
                <div className="space-y-1">
                  <p>
                    <span className="font-medium">Other paper:</span> {otherFinding}
                  </p>
                  <blockquote className="border-l-2 border-muted-foreground/30 pl-2 text-muted-foreground">
                    &ldquo;{otherQuote}&rdquo; <span>({pageLabel(otherPage)})</span>
                  </blockquote>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { errorMessage } from '@/lib/errors';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useJobStore } from '@/stores/job-store';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { MarkerCaption } from '@/components/typography/MarkerCaption';
import { EmptyState } from '@/components/EmptyState';
import { ConsensusMeter } from '@/components/consensus/ConsensusMeter';
import { fetchConsensus, scanContradictions } from '@/lib/api';
import type { ConsensusClaim } from '@/types';
import type { Job } from '@/stores/job-store';

function stanceTone(stance: string): string {
  return stance === 'supports' ? 'text-emerald-600' : 'text-red-600';
}

function isLibraryConsensusScan(job: Job): boolean {
  return job.kind === 'contradictions.scan' && job.payload?.paper_id == null;
}

function isActiveLibraryConsensusScan(job: Job): boolean {
  return isLibraryConsensusScan(job) && (job.status === 'running' || job.status === 'queued');
}

function latestLibraryConsensusScan(jobs: Record<string, Job>): Job | null {
  const scans = Object.values(jobs).filter(isLibraryConsensusScan);
  scans.sort((a, b) => {
    const aTime = a.finished_at ?? a.started_at ?? a.created_at;
    const bTime = b.finished_at ?? b.started_at ?? b.created_at;
    return bTime.localeCompare(aTime);
  });
  return scans[0] ?? null;
}

function ClaimEvidence({ claim }: { claim: ConsensusClaim }) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2 text-base">
          <span className="truncate">{claim.claim_topic}</span>
          <span className="shrink-0 font-mono text-xs text-meta">
            <span className="text-emerald-600">{claim.supports} support</span>
            {' · '}
            <span className="text-red-600">{claim.opposes} oppose</span>
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="text-sm text-meta transition-colors hover:text-strong"
          aria-expanded={open}
        >
          {open ? 'Hide evidence' : `Show evidence (${claim.assessments.length})`}
        </button>
        {open && (
          <ul className="space-y-3">
            {claim.assessments.map((assessment, index) => (
              <li key={index} className="border-l-2 border-hair pl-3 text-sm">
                <span className={`font-mono text-xs uppercase ${stanceTone(assessment.stance)}`}>
                  {assessment.stance}
                </span>
                <p className="mt-1">
                  <span className="text-meta">{assessment.paper_a_title}</span>
                  {assessment.page_a != null ? ` (p.${assessment.page_a})` : ''}: “
                  {assessment.quote_a}”
                </p>
                <p className="mt-1">
                  <span className="text-meta">{assessment.paper_b_title}</span>
                  {assessment.page_b != null ? ` (p.${assessment.page_b})` : ''}: “
                  {assessment.quote_b}”
                </p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export function ConsensusPage() {
  const consensusQuery = useQuery({
    queryKey: QUERY_KEYS.consensus.all(),
    queryFn: fetchConsensus,
    staleTime: 5 * 60_000,
  });

  const trackExternalJob = useJobStore((s) => s.trackExternalJob);
  const isScanning = useJobStore((s) => Object.values(s.jobs).some(isActiveLibraryConsensusScan));
  const jobs = useJobStore((s) => s.jobs);
  const latestScan = useMemo(() => latestLibraryConsensusScan(jobs), [jobs]);
  const scanMutation = useMutation({
    mutationFn: () => scanContradictions(),
    onSuccess: (r) =>
      trackExternalJob({
        jobId: r.job_id,
        kind: 'contradictions.scan',
        payload: {},
        status: r.status === 'running' || r.status === 'queued' ? r.status : 'queued',
      }),
  });
  const pending = scanMutation.isPending || isScanning;

  const claims = consensusQuery.data?.claims ?? [];
  const scanFailed = latestScan?.status === 'failed';
  const scanSucceeded = latestScan?.status === 'succeeded';
  const emptyTitle = scanSucceeded
    ? 'No consensus clusters found'
    : 'No related-paper claims yet';
  const emptyDescription = scanFailed
    ? 'The last contradiction scan failed before consensus data could be refreshed.'
    : scanSucceeded
      ? 'The scan finished, but the current library did not produce verified agreement or contradiction clusters.'
      : 'Run a contradiction scan across related papers to see where they agree and disagree.';

  return (
    <div className="space-y-6 p-6">
      <nav className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-faint">
        <span>Read</span>
        <span>/</span>
        <Link to="/consensus" className="text-meta transition-colors hover:text-strong">
          Consensus
        </Link>
      </nav>

      <MarkerCaption marker="CONSENSUS" />

      <div className="space-y-1">
        <h1 className="font-serif text-[2.5rem] leading-none tracking-tight text-strong">
          Consensus
        </h1>
        <p className="font-serif text-base italic text-muted-foreground">
          Where the papers in your library agree and disagree on shared claims.
        </p>
      </div>

      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <CardTitle className="text-lg">Agreement by claim</CardTitle>
        </CardHeader>
        <CardContent>
          {consensusQuery.isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-[200px] w-full" />
            </div>
          ) : consensusQuery.isError ? (
            <p className="text-sm text-destructive">
              Failed to load consensus: {errorMessage(consensusQuery.error)}
            </p>
          ) : claims.length > 0 ? (
            <ConsensusMeter data={claims} />
          ) : (
            <>
              <EmptyState
                title={emptyTitle}
                description={emptyDescription}
                actionLabel={pending ? 'Scanning…' : 'Run consensus scan'}
                onAction={() => {
                  if (!pending) scanMutation.mutate();
                }}
              />
              {(scanMutation.isError || scanFailed) && (
                <p className="mt-2 text-center text-sm text-destructive">
                  {scanFailed
                    ? (latestScan.error?.message ?? 'Consensus scan failed.')
                    : errorMessage(scanMutation.error, 'Failed to queue consensus scan.')}
                </p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {claims.length > 0 && (
        <>
          <MarkerCaption marker="EVIDENCE" />
          {scanFailed && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
            >
              <p className="font-medium">The latest consensus scan failed.</p>
              <p className="mt-1">
                Displayed claims may be stale. {latestScan.error?.message ?? 'Run the scan again after checking model and retrieval health.'}
              </p>
            </div>
          )}
          <div className="space-y-4">
            {claims.map((claim, index) => (
              <ClaimEvidence key={`${index}-${claim.claim_topic}`} claim={claim} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

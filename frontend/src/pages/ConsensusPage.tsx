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

interface ScanCounts {
  hasCounts: boolean;
  candidates: number;
  verificationFailures: number;
  contradictions: number;
}

/** Read the scan job's result diagnostics defensively (fields may be absent). */
function scanResultCounts(scan: Job | null): ScanCounts {
  const result = scan?.result ?? null;
  return {
    hasCounts: result != null && 'candidate_count' in result,
    candidates: Number(result?.candidate_count ?? 0),
    verificationFailures: Number(result?.verification_failures ?? 0),
    contradictions: Number(result?.contradictions_found ?? 0),
  };
}

/** Explain WHY a succeeded scan produced no consensus clusters. */
function succeededScanDescription(counts: ScanCounts): string {
  if (counts.hasCounts && counts.candidates === 0) {
    return 'Consensus needs papers that are summarized AND cross-referenced as related. None of your processed papers are cross-referenced yet.';
  }
  // Only claim "none passed verification" when EVERY candidate failed verification
  // (no model failures, no neutrals) — otherwise the wording misleads; the counts
  // line below shows the real breakdown for mixed outcomes.
  if (
    counts.hasCounts &&
    counts.candidates > 0 &&
    counts.contradictions === 0 &&
    counts.verificationFailures === counts.candidates
  ) {
    const pairs = counts.candidates === 1 ? 'pair' : 'pairs';
    return `Found ${counts.candidates} candidate ${pairs}; none passed quote verification.`;
  }
  return 'The scan finished, but the current library did not produce verified agreement or contradiction clusters.';
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
  const [scanSkipped, setScanSkipped] = useState(false);
  const scanMutation = useMutation({
    mutationFn: () => scanContradictions(),
    onSuccess: (r) => {
      if (r.status === 'skipped' || r.job_id == null) {
        // The backend refused to queue a guaranteed-empty scan — nothing to track.
        setScanSkipped(true);
        return;
      }
      setScanSkipped(false);
      trackExternalJob({
        jobId: r.job_id,
        kind: 'contradictions.scan',
        payload: {},
        status: r.status === 'running' || r.status === 'queued' ? r.status : 'queued',
      });
    },
  });
  const pending = scanMutation.isPending || isScanning;

  const claims = consensusQuery.data?.claims ?? [];
  const scanFailed = latestScan?.status === 'failed';
  const scanSucceeded = latestScan?.status === 'succeeded';
  const scanCounts = scanResultCounts(scanSucceeded ? latestScan : null);
  const emptyTitle = scanSkipped
    ? 'Nothing to scan yet'
    : scanSucceeded
      ? 'No consensus clusters found'
      : 'No related-paper claims yet';
  const emptyDescription = scanSkipped
    ? 'Process some papers first — consensus needs summarized papers with verified findings.'
    : scanFailed
      ? 'The last contradiction scan failed before consensus data could be refreshed.'
      : scanSucceeded
        ? succeededScanDescription(scanCounts)
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
          Where the papers in your library agree and disagree on shared claims. Works on papers
          that are processed and cross-referenced.
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
              {scanSucceeded && scanCounts.hasCounts && scanCounts.candidates > 0 && (
                <p className="mt-2 text-center font-mono text-xs text-meta">
                  {scanCounts.candidates} candidate pairs · {scanCounts.verificationFailures}{' '}
                  verification failures · {scanCounts.contradictions} verified contradictions
                </p>
              )}
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

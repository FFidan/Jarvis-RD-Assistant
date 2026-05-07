import { useCallback } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { useJobStore } from '@/stores/job-store';
import {
  fetchFeedPapers,
  fetchMissingFoundationalPapers,
  fetchAndProcessFoundationalPaper,
} from '@/lib/api';
import type { FeedPaper, MissingFoundationalPaper } from '@/types';

// ---------------------------------------------------------------------------
// Pill component — small rounded label for the row type column
// ---------------------------------------------------------------------------

type PillVariant = 'warn' | 'neutral';

function Pill({ label, variant }: { label: string; variant: PillVariant }) {
  const base =
    'text-[10px] font-mono uppercase tracking-wide px-2 py-0.5 rounded border whitespace-nowrap';
  const colors =
    variant === 'warn'
      ? 'bg-[hsl(var(--badge-warn-bg))] text-[hsl(var(--badge-warn-fg))] border-[hsl(var(--badge-warn-border))]'
      : 'bg-zinc-100 dark:bg-zinc-800 text-soft border-hair';
  return <span className={`${base} ${colors}`}>{label}</span>;
}

// ---------------------------------------------------------------------------
// Row sub-components
// ---------------------------------------------------------------------------

function FoundationalRow({
  paper,
  onAdd,
  isPending,
}: {
  paper: MissingFoundationalPaper;
  onAdd: (paperId: number) => void;
  isPending: boolean;
}) {
  return (
    <div className="grid grid-cols-[110px_1fr_auto] gap-3 items-center py-2.5 border-b border-hair last:border-0">
      {/* Col 1 — pill */}
      <Pill label="Foundational" variant="warn" />

      {/* Col 2 — title + meta */}
      <div className="min-w-0">
        <p className="text-[13px] text-zinc-800 dark:text-zinc-200 truncate font-medium leading-snug" title={paper.title}>
          {paper.title}
        </p>
        <p className="font-mono text-[10px] text-meta mt-0.5">
          cited by {paper.citation_count} papers
        </p>
      </div>

      {/* Col 3 — action */}
      <button
        onClick={() => onAdd(paper.paper_id)}
        disabled={isPending}
        className="shrink-0 flex items-center gap-1 text-[11px] font-mono text-[var(--ink-blue)] hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        Add &amp; process
      </button>
    </div>
  );
}

function ActionItemRow({
  paper,
  onProcess,
  isJobRunning,
}: {
  paper: FeedPaper;
  onProcess: (paper: FeedPaper) => void;
  isJobRunning: boolean;
}) {
  const pillLabel = !paper.pdf_downloaded ? 'No PDF' : 'Needs index';
  const pillVariant: PillVariant = !paper.pdf_downloaded ? 'warn' : 'neutral';

  const meta = [paper.source_type, paper.external_id].filter(Boolean).join(' · ');

  return (
    <div className="grid grid-cols-[110px_1fr_auto] gap-3 items-center py-2.5 border-b border-hair last:border-0">
      {/* Col 1 — pill */}
      <Pill label={pillLabel} variant={pillVariant} />

      {/* Col 2 — title + meta */}
      <div className="min-w-0">
        <p className="text-[13px] text-zinc-800 dark:text-zinc-200 truncate font-medium leading-snug" title={paper.title}>
          {paper.title}
        </p>
        <p className="font-mono text-[10px] text-meta mt-0.5">{meta}</p>
      </div>

      {/* Col 3 — action (only shown when pdf_downloaded) */}
      {paper.pdf_downloaded ? (
        <button
          onClick={() => onProcess(paper)}
          disabled={isJobRunning}
          className="shrink-0 text-[11px] font-mono text-[var(--ink-blue)] hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Process
        </button>
      ) : (
        /* No PDF — no action available; render an empty placeholder to preserve grid */
        <span />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TriageSection
// ---------------------------------------------------------------------------

export function TriageSection() {
  const queryClient = useQueryClient();
  const startJob = useJobStore((s) => s.startJob);
  const isRunning = useJobStore((s) => s.isRunning);
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);

  // Query 1 — unprocessed action items (reuses existing query key for dedup)
  const { data: actionData } = useQuery({
    queryKey: ['action-items-unprocessed'],
    queryFn: () => fetchFeedPapers({ statuses: 'new', limit: 10 }),
    refetchInterval: 60_000,
  });

  // Query 2 — missing foundational papers (reuses existing query key for dedup)
  const { data: foundationalData = [] } = useQuery({
    queryKey: ['analytics', 'missing-foundational'],
    queryFn: fetchMissingFoundationalPapers,
  });

  const actionItems: FeedPaper[] = actionData?.papers ?? [];
  const foundational: MissingFoundationalPaper[] = foundationalData;

  // Mutation for foundational "Add & process"
  // NOTE: must be declared before any conditional return to satisfy Rules of Hooks.
  const addMut = useMutation({
    mutationFn: (paperId: number) => fetchAndProcessFoundationalPaper(paperId),
    onSuccess: (result) => {
      if (result.job_id) {
        trackExternalJob({
          jobId: result.job_id,
          kind: result.status === 'queued' ? 'paper.analyze' : 'paper.process',
          payload: { paper_id: result.paper_id },
          status: 'queued',
        });
      }
      queryClient.invalidateQueries({ queryKey: ['analytics', 'missing-foundational'] });
    },
  });

  // "Process all" counts action items where pdf is downloaded and no job running
  // NOTE: derived before useCallback so the deps array is stable.
  const processableItems = actionItems.filter(
    (p) => p.pdf_downloaded && !isRunning('paper.process', { paper_id: p.id }),
  );
  const processableCount = processableItems.length;

  // NOTE: useCallback hooks must be declared before any conditional return.
  const handleProcessAll = useCallback(async () => {
    await Promise.all(
      processableItems.map((p) =>
        startJob('paper.process', { paper_id: p.id }).catch(() => {}),
      ),
    );
    queryClient.invalidateQueries({ queryKey: ['action-items-unprocessed'] });
  }, [processableItems, startJob, queryClient]);

  const handleProcess = useCallback(
    (paper: FeedPaper) => {
      startJob('paper.process', { paper_id: paper.id }).catch(() => {});
      queryClient.invalidateQueries({ queryKey: ['action-items-unprocessed'] });
    },
    [startJob, queryClient],
  );

  const totalCount = actionItems.length + foundational.length;

  // Empty state: return null per SPEC §"States & edge cases" → "Triage empty"
  if (actionItems.length === 0 && foundational.length === 0) return null;

  return (
    <section id="triage">
      <SectionHeader
        marker="Triage"
        meta={`${totalCount} item${totalCount !== 1 ? 's' : ''}`}
        right={
          processableCount > 0 ? (
            <Button
              variant="outline"
              size="sm"
              onClick={handleProcessAll}
            >
              Process all ({processableCount})
            </Button>
          ) : undefined
        }
      />

      <Card className="rounded-lg border border-hair">
        <CardContent className="p-4">
          {/* Foundational rows first */}
          {foundational.map((paper) => (
            <FoundationalRow
              key={`foundational-${paper.paper_id}`}
              paper={paper}
              onAdd={(paperId) => addMut.mutate(paperId)}
              isPending={addMut.isPending && addMut.variables === paper.paper_id}
            />
          ))}

          {/* Action item rows */}
          {actionItems.map((paper) => (
            <ActionItemRow
              key={`action-${paper.id}`}
              paper={paper}
              onProcess={handleProcess}
              isJobRunning={isRunning('paper.process', { paper_id: paper.id })}
            />
          ))}
        </CardContent>
      </Card>
    </section>
  );
}

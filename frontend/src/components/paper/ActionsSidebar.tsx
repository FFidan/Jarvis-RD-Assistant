import { useState, useRef, useCallback, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Download, Cog, FileText, Sparkles, Wand2, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { downloadPdf, processPdf, summarizePaper, generateCardsJob, fetchDecks } from '@/lib/api';
import { isProcessingFailed } from '@/lib/paper-pipeline';
import { useJobStore, type Job } from '@/stores/job-store';
import { useResearchMilestoneStore } from '@/stores/research-milestone-store';
import { streamAnalyze } from '@/lib/sse';
import { isSafeRelativeHref } from '@/lib/safe-href';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { FeedbackButtons } from '@/components/shared/FeedbackButtons';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import { errorMessage } from '@/lib/errors';
import type { RecentFeedback } from '@/types';

const ACTION_TOOLTIPS: Record<string, string> = {
  analyze:
    'Do everything in one click: download the PDF, extract passages, and generate a summary.',
  download:
    'Download the PDF from its source URL to local storage. Required before processing.',
  process:
    'Parse the PDF text, extract passages, and prepare for search. Required before chat, summary, or flashcards.',
  summarize:
    'Generate an LLM summary with verbatim evidence quotes and page numbers. Saved to the Summary tab.',
  'generate-cards':
    'Turn this paper into spaced-repetition flashcards. Requires the paper to be processed first.',
};

interface ActionsSidebarProps {
  paperId: number;
  /** Whether the paper's PDF has been downloaded */
  pdfDownloaded?: boolean;
  /** Whether the paper has been processed (chunks exist) */
  hasChunks?: boolean;
  /** Whether the paper has a summary */
  hasSummary?: boolean;
  /** Set when a persisted processing job on this paper ended in failure. */
  processingFailed?: boolean;
  /** Briefly pulse the Process PDF button (triggered by ?action=process query param) */
  pulseProcessButton?: boolean;
  /** Briefly pulse the Analyze Paper button (triggered by ?action=analyze query param) */
  pulseAnalyzeButton?: boolean;
  /** discovery_origin used to gate the Recommendation Feedback section. */
  discoveryOrigin?: 'user_initiated' | 'pulse' | 'recommender' | 'citation_batch';
  /** Last feedback signal (highlights the active thumb). */
  recentFeedback?: RecentFeedback | null;
  /** Lifecycle state — feedback section is hidden when state='trash'. */
  state?: string;
}

type AnalyzeStep = null | 'downloading' | 'processing' | 'summarizing';

const ANALYZE_STEPS = [
  { key: 'downloading', label: 'Downloading PDF' },
  { key: 'processing', label: 'Processing' },
  { key: 'summarizing', label: 'Generating summary' },
] as const;

type StepStatus = 'pending' | 'active' | 'completed' | 'skipped' | 'failed';

const STEP_STATUS: Record<string, StepStatus> = {
  started: 'active',
  completed: 'completed',
  skipped: 'skipped',
  failed: 'failed',
};

const TERMINAL_STATUSES: Job['status'][] = ['succeeded', 'failed', 'cancelled'];

export function ActionsSidebar({
  paperId,
  pdfDownloaded = false,
  hasChunks = false,
  hasSummary = false,
  processingFailed = false,
  pulseProcessButton = false,
  pulseAnalyzeButton = false,
  discoveryOrigin = 'user_initiated',
  recentFeedback = null,
  state = 'inbox',
}: ActionsSidebarProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);
  const isRunning = useJobStore((s) => s.isRunning);
  const recordResearchMilestone = useResearchMilestoneStore(
    (store) => store.recordMilestone,
  );
  const [genJobId, setGenJobId] = useState<string | null>(null);
  const genJob = useJobStore((s) => (genJobId ? s.jobs[genJobId] ?? null : null));
  const [deckId, setDeckId] = useState<string>('');
  const [maxCards, setMaxCards] = useState('5');
  const [actionResult, setActionResult] = useState<{ type: 'success' | 'error'; message: string; action_link?: { label: string; href: string } } | null>(null);
  const [analyzeStep, setAnalyzeStep] = useState<AnalyzeStep>(null);
  const [stepStatuses, setStepStatuses] = useState<Record<string, StepStatus>>({});
  const [stepReasons, setStepReasons] = useState<Record<string, string>>({});
  const [chunkCount, setChunkCount] = useState<number | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  /** The stage that last failed during a streamAnalyze run — used for per-stage Retry. */
  const [failedStage, setFailedStage] = useState<'downloading' | 'processing' | 'summarizing' | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

  // Restore terminal feedback for the tracked card.generate job. The store
  // (job-store.ts:222-253) only invalidates queries (success) or fires a
  // transient toast (failure); it does NOT set the persistent banner. Mirror
  // the old poll-loop terminal feedback here, then stop watching this job.
  useEffect(() => {
    if (!genJob || !TERMINAL_STATUSES.includes(genJob.status)) return;
    if (genJob.status === 'succeeded') {
      const r = (genJob.result ?? {}) as { cards_created?: number; confidence?: string | number };
      if (r.cards_created === 0) {
        toast.warning(
          'No reliable cards could be generated from this paper. Try Regenerate Summary first, or check that a capable model is configured (Settings → Models).',
        );
        setActionResult({
          type: 'error',
          message: 'No cards were generated. Try Regenerate Summary first.',
        });
        setShowAdvanced(true);
      } else {
        const detail = r.cards_created != null
          ? `Generated ${r.cards_created} cards${r.confidence != null ? ` (confidence: ${r.confidence})` : ''}`
          : 'Cards generated';
        setActionResult({ type: 'success', message: detail });
      }
    } else if (genJob.status === 'failed') {
      setActionResult({
        type: 'error',
        message: genJob.error?.message ?? 'Generation failed',
        action_link: genJob.error?.action_link,
      });
    }
    setGenJobId(null);
    // Depend on the status transition only — `genJob` identity churns on every
    // progress frame, but the banner should fire once at terminal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [genJob?.status]);

  const { data: decks = [], isError: decksError } = useQuery({
    queryKey: QUERY_KEYS.decks.list(),
    queryFn: fetchDecks,
  });

  const runAnalyze = useCallback(async () => {
    setIsAnalyzing(true);
    setActionResult(null);
    setChunkCount(null);
    setFailedStage(null);
    setStepStatuses({
      downloading: 'pending',
      processing: 'pending',
      summarizing: 'pending',
    });

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      for await (const event of streamAnalyze(paperId, controller.signal)) {
        if (event.type === 'step') {
          setAnalyzeStep(event.step);
          setStepStatuses((prev) => {
            const mapped = STEP_STATUS[event.status];
            return mapped ? { ...prev, [event.step]: mapped } : prev;
          });
          if (event.status === 'skipped' && event.reason) {
            setStepReasons((prev) => ({ ...prev, [event.step]: event.reason as string }));
          }
          if (event.step === 'processing' && event.status === 'completed' && event.chunk_count != null) {
            setChunkCount(event.chunk_count);
          }
        } else if (event.type === 'complete') {
          recordResearchMilestone('analyze');
          setActionResult({ type: 'success', message: 'Analysis complete' });
          queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.detail(paperId) });
          toast.success('Analyzed! You can now Ask across your library', {
            action: { label: 'Go to Ask', onClick: () => navigate('/ask') },
          });
        } else if (event.type === 'error') {
          const failedStep = event.step || 'analysis';
          const stage = (event.step as 'downloading' | 'processing' | 'summarizing') || null;
          setFailedStage(stage);
          setStepStatuses((prev) => ({
            ...prev,
            ...(event.step ? { [event.step]: 'failed' as StepStatus } : {}),
          }));
          // Prefer sanitized backend display fields; fall back to legacy raw fields for old services.
          const structuredMsg =
            event.error_code && event.display_message
              ? `${event.error_code}: ${event.display_message}`
              : event.display_message
              ? event.display_message
              : event.error_code
              ? event.error_code
              : event.error_type && event.error_detail
              ? `${event.error_type}: ${event.error_detail}`
              : event.error_type
              ? event.error_type
              : `Failed during ${failedStep}: ${event.message}`;
          setActionResult({
            type: 'error',
            message: structuredMsg,
          });
        }
      }
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        setActionResult({
          type: 'error',
          message: `Analysis failed: ${errorMessage(err)}`,
        });
      }
    } finally {
      setIsAnalyzing(false);
      setAnalyzeStep(null);
      abortRef.current = null;
    }
  }, [paperId, queryClient, navigate, recordResearchMilestone]);

  const downloadMut = useMutation({
    mutationFn: () => downloadPdf(paperId),
    onSuccess: () => {
      setActionResult({ type: 'success', message: 'PDF downloaded successfully' });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.detail(paperId) });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: errorMessage(err, 'Download failed') });
    },
  });

  const processMut = useMutation({
    mutationFn: () => processPdf(paperId),
    onSuccess: (data) => {
      trackExternalJob({
        jobId: data.job_id,
        kind: 'paper.process',
        payload: { paper_id: paperId },
        status: 'queued',
      });
      setActionResult({
        type: 'success',
        message: 'Processing queued — track progress in the jobs panel',
      });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: errorMessage(err, 'Processing failed') });
    },
  });

  const summarizeMut = useMutation({
    mutationFn: () => summarizePaper(paperId),
    onSuccess: (data) => {
      trackExternalJob({
        jobId: data.job_id,
        kind: 'paper.summarize',
        payload: { paper_id: paperId },
        status: 'queued',
      });
      setActionResult({ type: 'success', message: 'Summary queued — track progress in the jobs panel' });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: errorMessage(err, 'Summarization failed') });
    },
  });

  const regenerateSummarizeMut = useMutation({
    mutationFn: () => summarizePaper(paperId, { force: true }),
    onSuccess: (data) => {
      trackExternalJob({
        jobId: data.job_id,
        kind: 'paper.summarize',
        payload: { paper_id: paperId },
        status: 'queued',
      });
      setActionResult({ type: 'success', message: 'Summary queued — track progress in the jobs panel' });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: errorMessage(err, 'Regeneration failed') });
    },
  });

  const generateMut = useMutation({
    mutationFn: () => generateCardsJob(paperId, Number(deckId), Number(maxCards)),
    onSuccess: (data) => {
      setActionResult(null);
      const id = trackExternalJob({
        jobId: data.job_id,
        kind: 'card.generate',
        payload: { paper_id: paperId, deck_id: Number(deckId) },
        status: 'queued',
      });
      setGenJobId(id);
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: errorMessage(err, 'Generation failed') });
    },
  });

  const isGenPending = generateMut.isPending || isRunning('card.generate', { paper_id: paperId, deck_id: Number(deckId) });
  const anyPending = downloadMut.isPending || processMut.isPending || summarizeMut.isPending || regenerateSummarizeMut.isPending || isGenPending || isAnalyzing;

  const analyzeLabel = (() => {
    switch (analyzeStep) {
      case 'downloading': return 'Downloading PDF...';
      case 'processing': return 'Extracting passages…';
      case 'summarizing': return 'Summarizing...';
      default: return 'Analyze Paper';
    }
  })();

  // Fully analysed papers need a status, not a call to action: a loud
  // "Analyze Paper" button on finished work reads as something left to do.
  // Regenerate stays available under "Show advanced".
  const fullyAnalyzed = pdfDownloaded && hasChunks && hasSummary && !isAnalyzing;

  return (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold">Actions</h3>

      {fullyAnalyzed ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <CheckCircle2 className="h-4 w-4 text-[var(--status-ok)]" aria-hidden="true" />
          Analyzed — passages extracted{chunkCount != null ? ` (${chunkCount})` : ''}, summary ready.
        </p>
      ) : (
      <>
      <Button
        id="paper-action-analyze"
        variant="default"
        className={`w-full justify-start${pulseAnalyzeButton ? ' animate-pulse' : ''}`}
        onClick={() => { setActionResult(null); runAnalyze(); }}
        disabled={anyPending}
      >
        <Wand2 className="mr-2 h-4 w-4" />
        {analyzeLabel}
        <InfoTooltip
          content={ACTION_TOOLTIPS['analyze']}
          side="left"
          className="ml-auto"
          triggerElement="span"
        />
      </Button>

      {/* Step tracker: visible while there is pipeline work to see through */}
      <div className="space-y-2 rounded-md border p-3">
          {ANALYZE_STEPS.map((step) => {
            // During an active analyze run, use live stepStatuses.
            // Otherwise derive state from paper props via the shared pipeline selector.
            let status: StepStatus = stepStatuses[step.key] || 'pending';
            if (!isAnalyzing && !Object.values(stepStatuses).some((s) => s !== 'pending')) {
              if (step.key === 'downloading') status = pdfDownloaded ? 'completed' : 'pending';
              else if (step.key === 'processing')
                status = isProcessingFailed({ processingFailed, hasChunks })
                  ? 'failed'
                  : hasChunks
                    ? 'completed'
                    : 'pending';
              else if (step.key === 'summarizing') status = hasSummary ? 'completed' : 'pending';
            }
            const isFailed = status === 'failed';
            const isDone = status === 'completed';
            const isCurrent = status === 'active';
            const Icon = isFailed ? XCircle : isDone ? CheckCircle2 : isCurrent ? Loader2 : null;
            const isSkipped = status === 'skipped';
            const label = isSkipped
              ? `${step.label} — Skipped${stepReasons[step.key] ? ` (${stepReasons[step.key]})` : ''}`
              : step.key === 'processing' && isDone && chunkCount != null
                ? `${step.label} (${chunkCount} chunks)`
                : step.label;
            return (
              <div key={step.key} className="flex items-center gap-2 text-sm">
                {Icon ? (
                  <Icon className={`h-4 w-4 ${isFailed ? 'text-destructive' : isDone ? 'text-[var(--status-ok)]' : 'animate-spin text-blue-500'}`} />
                ) : (
                  <div className="h-4 w-4 rounded-full border" />
                )}
                {/* Done steps are muted, not struck through — strikethrough
                    conventionally reads as cancelled, not completed */}
                <span className={isFailed ? 'font-medium text-destructive' : isCurrent ? 'font-medium' : 'text-muted-foreground'}>
                  {label}
                </span>
              </div>
            );
          })}
      </div>
      </>
      )}

      <div className="mt-2">
        <button
          type="button"
          className="cursor-pointer text-xs text-muted-foreground hover:text-foreground select-none"
          onClick={() => setShowAdvanced((v) => !v)}
          aria-expanded={showAdvanced}
        >
          Show advanced {showAdvanced ? '▴' : '▾'}
        </button>
        {showAdvanced && (
          <div className="mt-2 flex flex-col gap-1">
            {/* Download PDF — hidden once PDF is already downloaded */}
            {!pdfDownloaded && (
              <Button
                variant="outline"
                size="sm"
                className="w-full justify-start"
                onClick={() => { setActionResult(null); downloadMut.mutate(); }}
                disabled={anyPending}
              >
                <Download className="mr-2 h-4 w-4" />
                {downloadMut.isPending ? 'Downloading...' : 'Download PDF'}
                <InfoTooltip
                  content={ACTION_TOOLTIPS['download']}
                  side="left"
                  className="ml-auto"
                  triggerElement="span"
                />
              </Button>
            )}

            {/* Process PDF — hidden once paper has chunks */}
            {pdfDownloaded && !hasChunks && (
              <Button
                id="paper-action-process"
                variant="outline"
                size="sm"
                className={`w-full justify-start${pulseProcessButton ? ' animate-pulse' : ''}`}
                onClick={() => { setActionResult(null); processMut.mutate(); }}
                disabled={anyPending}
              >
                <Cog className="mr-2 h-4 w-4" />
                {processMut.isPending ? 'Processing...' : 'Process PDF'}
                <InfoTooltip
                  content={ACTION_TOOLTIPS['process']}
                  side="left"
                  className="ml-auto"
                  triggerElement="span"
                />
              </Button>
            )}

            {/* Generate Summary — hidden once summary exists */}
            {hasChunks && !hasSummary && (
              <Button
                variant="outline"
                size="sm"
                className="w-full justify-start"
                onClick={() => { setActionResult(null); summarizeMut.mutate(); }}
                disabled={anyPending}
              >
                <FileText className="mr-2 h-4 w-4" />
                {summarizeMut.isPending ? 'Summarizing...' : 'Generate Summary'}
                <InfoTooltip
                  content={ACTION_TOOLTIPS['summarize']}
                  side="left"
                  className="ml-auto"
                  triggerElement="span"
                />
              </Button>
            )}

            {hasChunks && hasSummary && (
              <Button
                variant="outline"
                size="sm"
                className="w-full justify-start"
                onClick={() => { setActionResult(null); regenerateSummarizeMut.mutate(); }}
                disabled={anyPending}
              >
                <FileText className="mr-2 h-4 w-4" />
                {regenerateSummarizeMut.isPending ? 'Regenerating...' : 'Regenerate Summary'}
              </Button>
            )}

            {/* If all three stages are done, indicate nothing more to do */}
            {pdfDownloaded && hasChunks && hasSummary && (
              <p className="text-xs text-muted-foreground py-1">All pipeline stages complete.</p>
            )}
          </div>
        )}
      </div>

      {actionResult && (
        <div className={`text-sm rounded-md border p-2 ${actionResult.type === 'error' ? 'border-destructive/30 bg-destructive/10 text-destructive' : 'border-green-500/30 bg-green-500/10 text-[var(--status-ok)]'}`}>
          <p className="font-medium">{actionResult.message}</p>
          {actionResult.action_link &&
            (isSafeRelativeHref(actionResult.action_link.href) ? (
              <Link
                to={actionResult.action_link.href}
                className="underline hover:opacity-80 text-xs mt-1 block"
              >
                {actionResult.action_link.label}
              </Link>
            ) : (
              <span className="text-xs mt-1 block">{actionResult.action_link.label}</span>
            ))}
          {/* Per-stage Retry button — only shown for SSE pipeline errors */}
          {actionResult.type === 'error' && failedStage && (
            <Button
              variant="outline"
              size="sm"
              className="mt-2 h-7 px-2 text-xs"
              disabled={anyPending}
              onClick={() => {
                setActionResult(null);
                if (failedStage === 'downloading') downloadMut.mutate();
                else if (failedStage === 'processing') processMut.mutate();
                else if (failedStage === 'summarizing') summarizeMut.mutate();
              }}
            >
              Retry {failedStage === 'downloading' ? 'download' : failedStage === 'processing' ? 'processing' : 'summarize'}
            </Button>
          )}
        </div>
      )}

      <Separator />

      {/* Recommendation Feedback section. */}
      {/* FeedbackButtons self-gates on discoveryOrigin === 'user_initiated'; this section
          hides entirely when state='trash' (no double-prompt for trashed papers). */}
      {state !== 'trash' && discoveryOrigin !== 'user_initiated' && (
        <>
          <div>
            <h3 className="text-lg font-semibold">Recommendation Feedback</h3>
            <p className="text-xs text-muted-foreground mt-0.5 mb-2">
              Tell the recommender whether this paper is on-target.
            </p>
            <FeedbackButtons
              paperId={paperId}
              discoveryOrigin={discoveryOrigin}
              source="paper_detail_thumbs"
              recentFeedback={recentFeedback}
              size="md"
              showReasonInput
            />
          </div>
          <Separator />
        </>
      )}

      <h3 className="text-lg font-semibold">Generate Cards</h3>

      {decksError ? (
        <QueryErrorState message="Failed to load decks." />
      ) : decks.length > 0 ? (
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="action-deck">Target Deck</Label>
            <Select value={deckId} onValueChange={setDeckId}>
              <SelectTrigger id="action-deck">
                <SelectValue placeholder="Select a deck" />
              </SelectTrigger>
              <SelectContent>
                {decks.map((d) => (
                  <SelectItem key={d.id} value={String(d.id)}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <div className="flex items-center gap-1.5">
              <Label htmlFor="action-max-cards">Max cards</Label>
              <InfoTooltip content="Maximum flashcards to generate from this paper. 5 is a sensible default — more cards cost more LLM tokens and take longer." />
            </div>
            <Input
              id="action-max-cards"
              type="number"
              min={1}
              max={20}
              value={maxCards}
              onChange={(e) => setMaxCards(e.target.value)}
            />
          </div>
          {genJob && !TERMINAL_STATUSES.includes(genJob.status) && (
            <div className="space-y-1">
              <p className="text-xs text-muted-foreground">
                {genJob.progress_message ?? (genJob.status === 'queued' ? 'Queued…' : 'Generating…')}
              </p>
              {genJob.progress != null && (
                <div className="h-1 w-full rounded-full bg-muted">
                  <div
                    className="h-1 rounded-full bg-primary transition-all"
                    style={{ width: `${Math.round((genJob.progress ?? 0) * 100)}%` }}
                  />
                </div>
              )}
            </div>
          )}
          <Button
            className="w-full"
            onClick={() => { setActionResult(null); generateMut.mutate(); }}
            disabled={!deckId || !hasChunks || anyPending}
          >
            <Sparkles className="mr-2 h-4 w-4" />
            {isGenPending ? 'Generating…' : 'Generate Cards'}
            <InfoTooltip
              content={ACTION_TOOLTIPS['generate-cards']}
              side="left"
              className="ml-auto"
              triggerElement="span"
            />
          </Button>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          No decks available. Create a deck in Learning Cards first.
        </p>
      )}
    </div>
  );
}

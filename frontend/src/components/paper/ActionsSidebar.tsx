import { useState, useRef, useCallback, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Cog, FileText, Sparkles, Wand2, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { downloadPdf, processPdf, summarizePaper, generateCardsJob, getJob, fetchDecks } from '@/lib/api';
import { useJobStore, type Job } from '@/stores/job-store';
import { streamAnalyze } from '@/lib/sse';
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
import type { RecentFeedback, PartialGenJob } from '@/types';

const ACTION_TOOLTIPS: Record<string, string> = {
  analyze:
    'Run the full pipeline: download the PDF, process it into chunks, and generate a summary. One click for everything.',
  download:
    'Download the PDF from its source URL to local storage. Required before processing.',
  process:
    'Parse the PDF text, split into chunks, and embed for semantic search. Required before chat, summary, or flashcards.',
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
  /** Briefly pulse the Process PDF button (triggered by ?action=process query param) */
  pulseProcessButton?: boolean;
  /** discovery_origin used to gate the Recommendation Feedback section (spec §5.2). */
  discoveryOrigin?: 'user_initiated' | 'pulse' | 'recommender' | 'citation_batch';
  /** Last feedback signal (highlights the active thumb). */
  recentFeedback?: RecentFeedback | null;
  /** Lifecycle state — feedback section is hidden when state='trash'. */
  state?: string;
}

type AnalyzeStep = null | 'downloading' | 'processing' | 'summarizing';

const ANALYZE_STEPS = [
  { key: 'downloading', label: 'Downloading PDF' },
  { key: 'processing', label: 'Processing & embedding' },
  { key: 'summarizing', label: 'Generating summary' },
] as const;

type StepStatus = 'pending' | 'active' | 'completed' | 'failed';

const TERMINAL_STATUSES: Job['status'][] = ['succeeded', 'failed', 'cancelled'];

export function ActionsSidebar({
  paperId,
  pdfDownloaded = false,
  hasChunks = false,
  hasSummary = false,
  pulseProcessButton = false,
  discoveryOrigin = 'user_initiated',
  recentFeedback = null,
  state = 'inbox',
}: ActionsSidebarProps) {
  const queryClient = useQueryClient();
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);
  const [deckId, setDeckId] = useState<string>('');
  const [maxCards, setMaxCards] = useState('5');
  const [actionResult, setActionResult] = useState<{ type: 'success' | 'error'; message: string; action_link?: { label: string; href: string } } | null>(null);
  const [analyzeStep, setAnalyzeStep] = useState<AnalyzeStep>(null);
  const [stepStatuses, setStepStatuses] = useState<Record<string, StepStatus>>({});
  const [chunkCount, setChunkCount] = useState<number | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  /** The stage that last failed during a streamAnalyze run — used for per-stage Retry. */
  const [failedStage, setFailedStage] = useState<'downloading' | 'processing' | 'summarizing' | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const genPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [genJob, setGenJob] = useState<Job | PartialGenJob | null>(null);

  useEffect(() => () => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    if (genPollRef.current !== null) {
      clearInterval(genPollRef.current);
      genPollRef.current = null;
    }
  }, []);

  const { data: decks = [] } = useQuery({
    queryKey: ['decks'],
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
          setStepStatuses((prev) => ({
            ...prev,
            [event.step]: event.status === 'started' ? 'active'
              : event.status === 'completed' ? 'completed'
              : 'failed',
          }));
          if (event.step === 'processing' && event.status === 'completed' && event.chunk_count != null) {
            setChunkCount(event.chunk_count);
          }
        } else if (event.type === 'complete') {
          setActionResult({ type: 'success', message: 'Analysis complete' });
          queryClient.invalidateQueries({ queryKey: ['paper-detail', paperId] });
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
          message: `Analysis failed: ${err instanceof Error ? err.message : 'Unknown error'}`,
        });
      }
    } finally {
      setIsAnalyzing(false);
      setAnalyzeStep(null);
      abortRef.current = null;
    }
  }, [paperId, queryClient]);

  const downloadMut = useMutation({
    mutationFn: () => downloadPdf(paperId),
    onSuccess: () => {
      setActionResult({ type: 'success', message: 'PDF downloaded successfully' });
      queryClient.invalidateQueries({ queryKey: ['paper-detail', paperId] });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: err instanceof Error ? err.message : 'Download failed' });
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
        message: `Processing queued (job ${data.job_id})`,
      });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: err instanceof Error ? err.message : 'Processing failed' });
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
      setActionResult({ type: 'success', message: `Summary queued (job ${data.job_id})` });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: err instanceof Error ? err.message : 'Summarization failed' });
    },
  });

  const stopGenPoll = () => {
    if (genPollRef.current !== null) {
      clearInterval(genPollRef.current);
      genPollRef.current = null;
    }
  };

  const startGenPoll = (id: string) => {
    stopGenPoll();
    genPollRef.current = setInterval(async () => {
      try {
        const row = await getJob(id);
        setGenJob(row);
        if (TERMINAL_STATUSES.includes(row.status)) {
          stopGenPoll();
          if (row.status === 'succeeded' && row.result) {
            const res = row.result as { cards_created?: number; confidence?: string };
            setActionResult({
              type: 'success',
              message: `Generated ${res.cards_created ?? '?'} cards (confidence: ${res.confidence ?? '?'})`,
            });
            queryClient.invalidateQueries({ queryKey: ['cards'] });
            queryClient.invalidateQueries({ queryKey: ['decks'] });
          } else if (row.status === 'failed') {
            const errPayload = row.error;
            const msg = errPayload?.message ?? 'Generation failed';
            setActionResult({
              type: 'error',
              message: msg,
              action_link: errPayload?.action_link,
            });
          }
          setGenJob(null);
        }
      } catch (err) {
        console.error('[ActionsSidebar] gen poll error', err);
      }
    }, 1000);
  };

  const generateMut = useMutation({
    mutationFn: () => generateCardsJob(paperId, Number(deckId), Number(maxCards)),
    onSuccess: (data) => {
      setActionResult(null);
      setGenJob({ status: 'queued' } satisfies PartialGenJob);
      startGenPoll(data.job_id);
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: err instanceof Error ? err.message : 'Generation failed' });
    },
  });

  const isGenPending = generateMut.isPending || (genJob !== null && !TERMINAL_STATUSES.includes(genJob.status));
  const anyPending = downloadMut.isPending || processMut.isPending || summarizeMut.isPending || isGenPending || isAnalyzing;

  const analyzeLabel = (() => {
    switch (analyzeStep) {
      case 'downloading': return 'Downloading PDF...';
      case 'processing': return 'Processing PDF...';
      case 'summarizing': return 'Summarizing...';
      default: return 'Analyze Paper';
    }
  })();

  return (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold">Actions</h3>

      <Button
        variant="default"
        className="w-full justify-start"
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

      {/* Step tracker: always visible so users can see pipeline completion state */}
      <div className="space-y-2 rounded-md border p-3">
          {ANALYZE_STEPS.map((step) => {
            // During an active analyze run, use live stepStatuses.
            // Otherwise derive state from paper props.
            let status: StepStatus = stepStatuses[step.key] || 'pending';
            if (!isAnalyzing && !Object.values(stepStatuses).some((s) => s !== 'pending')) {
              if (step.key === 'downloading') status = pdfDownloaded ? 'completed' : 'pending';
              else if (step.key === 'processing') status = hasChunks ? 'completed' : 'pending';
              else if (step.key === 'summarizing') status = hasSummary ? 'completed' : 'pending';
            }
            const isFailed = status === 'failed';
            const isDone = status === 'completed';
            const isCurrent = status === 'active';
            const Icon = isFailed ? XCircle : isDone ? CheckCircle2 : isCurrent ? Loader2 : null;
            const label = step.key === 'processing' && isDone && chunkCount != null
              ? `${step.label} (${chunkCount} chunks)`
              : step.label;
            return (
              <div key={step.key} className="flex items-center gap-2 text-sm">
                {Icon ? (
                  <Icon className={`h-4 w-4 ${isFailed ? 'text-destructive' : isDone ? 'text-[var(--status-ok)]' : 'animate-spin text-blue-500'}`} />
                ) : (
                  <div className="h-4 w-4 rounded-full border" />
                )}
                <span className={isFailed ? 'font-medium text-destructive' : isDone ? 'text-muted-foreground line-through' : isCurrent ? 'font-medium' : 'text-muted-foreground'}>
                  {label}
                </span>
              </div>
            );
          })}
      </div>

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
          {actionResult.action_link && (
            <Link
              to={actionResult.action_link.href}
              className="underline hover:opacity-80 text-xs mt-1 block"
            >
              {actionResult.action_link.label}
            </Link>
          )}
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

      {/* Recommendation Feedback section — spec §5.2 line 349. */}
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

      {decks.length > 0 ? (
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
                {('progress_message' in genJob ? genJob.progress_message : null) ?? (genJob.status === 'queued' ? 'Queued…' : 'Generating…')}
              </p>
              {'progress' in genJob && genJob.progress != null && (
                <div className="h-1 w-full rounded-full bg-muted">
                  <div
                    className="h-1 rounded-full bg-primary transition-all"
                    style={{ width: `${Math.round(genJob.progress * 100)}%` }}
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

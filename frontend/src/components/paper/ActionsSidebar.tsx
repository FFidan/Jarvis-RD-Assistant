import { useState, useRef, useCallback, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Cog, FileText, Sparkles, Wand2, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { downloadPdf, processPdf, summarizePaper, generateCardsJob, getJob, fetchDecks } from '@/lib/api';
import type { Job } from '@/stores/job-store';
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
}

type AnalyzeStep = null | 'downloading' | 'processing' | 'summarizing';

const ANALYZE_STEPS = [
  { key: 'downloading', label: 'Downloading PDF' },
  { key: 'processing', label: 'Processing & embedding' },
  { key: 'summarizing', label: 'Generating summary' },
] as const;

type StepStatus = 'pending' | 'active' | 'completed' | 'failed';

const TERMINAL_STATUSES: Job['status'][] = ['succeeded', 'failed', 'cancelled'];

export function ActionsSidebar({ paperId, pdfDownloaded = false, hasChunks = false, hasSummary = false, pulseProcessButton = false }: ActionsSidebarProps) {
  const queryClient = useQueryClient();
  const [deckId, setDeckId] = useState<string>('');
  const [maxCards, setMaxCards] = useState('5');
  const [actionResult, setActionResult] = useState<{ type: 'success' | 'error'; message: string; action_link?: { label: string; href: string } } | null>(null);
  const [analyzeStep, setAnalyzeStep] = useState<AnalyzeStep>(null);
  const [stepStatuses, setStepStatuses] = useState<Record<string, StepStatus>>({});
  const [chunkCount, setChunkCount] = useState<number | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const genPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [genJob, setGenJob] = useState<Job | null>(null);

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
          setStepStatuses((prev) => ({
            ...prev,
            ...(event.step ? { [event.step]: 'failed' as StepStatus } : {}),
          }));
          setActionResult({
            type: 'error',
            message: `Failed during ${failedStep}: ${event.message}`,
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
      setActionResult({
        type: 'success',
        message: `Processed: ${data.chunk_count} chunks created`,
      });
      queryClient.invalidateQueries({ queryKey: ['paper-detail', paperId] });
    },
    onError: (err) => {
      setActionResult({ type: 'error', message: err instanceof Error ? err.message : 'Processing failed' });
    },
  });

  const summarizeMut = useMutation({
    mutationFn: () => summarizePaper(paperId),
    onSuccess: () => {
      setActionResult({ type: 'success', message: 'Summary generated!' });
      queryClient.invalidateQueries({ queryKey: ['paper-detail', paperId] });
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
      setGenJob({ status: 'queued' } as unknown as Job);
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
                  <Icon className={`h-4 w-4 ${isFailed ? 'text-destructive' : isDone ? 'text-green-500' : 'animate-spin text-blue-500'}`} />
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

      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground select-none">
          Manual steps ▾
        </summary>
        <div className="mt-2 flex flex-col gap-1">
          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start"
            onClick={() => { setActionResult(null); downloadMut.mutate(); }}
            disabled={anyPending}
          >
            <Download className="mr-2 h-4 w-4" />
            {downloadMut.isPending ? 'Downloading...' : 'Download PDF'}
          </Button>

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
          </Button>

          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start"
            onClick={() => { setActionResult(null); summarizeMut.mutate(); }}
            disabled={anyPending}
          >
            <FileText className="mr-2 h-4 w-4" />
            {summarizeMut.isPending ? 'Summarizing...' : 'Generate Summary'}
          </Button>
        </div>
      </details>

      {actionResult && (
        <div className={`text-sm ${actionResult.type === 'error' ? 'text-destructive' : 'text-green-600'}`}>
          <p>{actionResult.message}</p>
          {actionResult.action_link && (
            <Link
              to={actionResult.action_link.href}
              className="underline hover:opacity-80"
            >
              {actionResult.action_link.label}
            </Link>
          )}
        </div>
      )}

      <Separator />

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
                {genJob.progress_message ?? (genJob.status === 'queued' ? 'Queued…' : 'Generating…')}
              </p>
              {genJob.progress != null && (
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
            disabled={!deckId || anyPending}
          >
            <Sparkles className="mr-2 h-4 w-4" />
            {isGenPending ? 'Generating…' : 'Generate Cards'}
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

import { useState, useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import { createCard, generateCardsJob, getJob, fetchDecks } from '@/lib/api';
import type { Job } from '@/stores/job-store';
import type { PartialGenJob } from '@/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { PaperSearchSelect } from '@/components/shared/PaperSearchSelect';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const CARD_TYPES = ['concept', 'quote', 'method', 'comparison'] as const;

interface CreateCardFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultDeckId?: number | null;
}

export function CreateCardForm({ open, onOpenChange, defaultDeckId }: CreateCardFormProps) {
  const queryClient = useQueryClient();
  const [front, setFront] = useState('');
  const [back, setBack] = useState('');
  const [cardType, setCardType] = useState<string>('concept');
  const [deckId, setDeckId] = useState<string>(() => (defaultDeckId != null ? String(defaultDeckId) : ''));

  const { data: decks = [] } = useQuery({
    queryKey: ['decks'],
    queryFn: fetchDecks,
  });

  const createMut = useMutation({
    mutationFn: () =>
      createCard({
        deck_id: Number(deckId),
        card_type: cardType,
        front,
        back,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cards'] });
      queryClient.invalidateQueries({ queryKey: ['decks'] });
      queryClient.invalidateQueries({ queryKey: ['card-stats'] });
      onOpenChange(false);
      setFront('');
      setBack('');
      setCardType('concept');
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Create Card</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label htmlFor="card-deck">Deck</Label>
            <Select value={deckId} onValueChange={setDeckId}>
              <SelectTrigger id="card-deck">
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
          <div className="space-y-2">
            <Label htmlFor="card-type">Type</Label>
            <Select value={cardType} onValueChange={setCardType}>
              <SelectTrigger id="card-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CARD_TYPES.map((t) => (
                  <SelectItem key={t} value={t} className="capitalize">
                    {t}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="card-front">Front (Question)</Label>
            <Textarea
              id="card-front"
              value={front}
              onChange={(e) => setFront(e.target.value)}
              placeholder="What is the question?"
              rows={3}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="card-back">Back (Answer)</Label>
            <Textarea
              id="card-back"
              value={back}
              onChange={(e) => setBack(e.target.value)}
              placeholder="What is the answer?"
              rows={3}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            onClick={() => createMut.mutate()}
            disabled={!front.trim() || !back.trim() || !deckId || createMut.isPending}
          >
            {createMut.isPending ? 'Creating...' : 'Create'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// --- Generate Cards Dialog ---

const TERMINAL_STATUSES: Job['status'][] = ['succeeded', 'failed', 'cancelled'];

interface GenerateCardsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultDeckId?: number | null;
}

export function GenerateCardsDialog({ open, onOpenChange, defaultDeckId }: GenerateCardsDialogProps) {
  const queryClient = useQueryClient();
  const [paperId, setPaperId] = useState('');
  const [deckId, setDeckId] = useState<string>(defaultDeckId ? String(defaultDeckId) : '');
  const [maxCards, setMaxCards] = useState('5');
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | PartialGenJob | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const { data: decks = [] } = useQuery({
    queryKey: ['decks'],
    queryFn: fetchDecks,
  });

  /** Stop polling interval if running. */
  const stopPolling = () => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  /** Start polling GET /api/jobs/{id} every 1s. */
  const startPolling = (id: string) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const row = await getJob(id);
        setJob(row);
        if (TERMINAL_STATUSES.includes(row.status)) {
          stopPolling();
          if (row.status === 'succeeded') {
            queryClient.invalidateQueries({ queryKey: ['cards'] });
            queryClient.invalidateQueries({ queryKey: ['decks'] });
            queryClient.invalidateQueries({ queryKey: ['card-stats'] });
          }
        }
      } catch (err) {
        console.error('[GenerateCardsDialog] poll error', err);
      }
    }, 1000);
  };

  /** Clean up interval on unmount or dialog close. */
  useEffect(() => () => stopPolling(), []); // eslint-disable-line react-hooks/exhaustive-deps

  const reset = () => {
    stopPolling();
    setJobId(null);
    setJob(null);
  };

  const genMut = useMutation({
    mutationFn: () => generateCardsJob(Number(paperId), Number(deckId), Number(maxCards)),
    onSuccess: (data) => {
      setJobId(data.job_id);
      setJob(null);
      startPolling(data.job_id);
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : 'Generation failed';
      setJob({ status: 'failed', error: { message: msg } } satisfies PartialGenJob);
    },
  });

  // Sync default deck when it changes
  useEffect(() => {
    if (defaultDeckId && !deckId) {
      setDeckId(String(defaultDeckId));
    }
  }, [defaultDeckId]); // eslint-disable-line react-hooks/exhaustive-deps

  const isGenerating = genMut.isPending || (!!jobId && (!job || !TERMINAL_STATUSES.includes(job.status)));
  const isQueued = !!jobId && (!job || job.status === 'queued');
  const isRunning = !!job && job.status === 'running';
  const isDone = !!job && TERMINAL_STATUSES.includes(job.status);

  const progressLabel = isQueued
    ? 'Queued…'
    : isRunning
    ? ((job && 'progress_message' in job ? job.progress_message : null) ?? 'Generating…')
    : null;

  const progressPct = isRunning && job && 'progress' in job && job.progress != null
    ? Math.round(job.progress * 100)
    : null;

  const successMsg = isDone && job?.status === 'succeeded' && job && 'result' in job && job.result
    ? `Generated ${(job.result as { cards_created?: number }).cards_created ?? '?'} cards (confidence: ${(job.result as { confidence?: string }).confidence ?? '?'})`
    : null;

  const errorPayload = isDone && job?.status === 'failed' && job
    ? (job.error as Job['error'] ?? { message: 'Unknown error' })
    : null;

  return (
    <Dialog open={open} onOpenChange={(v) => { onOpenChange(v); if (!v) reset(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5" /> Generate Cards from Paper
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label>Paper</Label>
            <PaperSearchSelect
              value={paperId ? Number(paperId) : null}
              onChange={(id) => setPaperId(id ? String(id) : '')}
              placeholder="Search for a paper..."
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="gen-deck">Deck</Label>
            <Select value={deckId} onValueChange={setDeckId}>
              <SelectTrigger id="gen-deck">
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
          <div className="space-y-2">
            <Label htmlFor="gen-max">Max Cards</Label>
            <Input
              id="gen-max"
              type="number"
              min={1}
              max={20}
              value={maxCards}
              onChange={(e) => setMaxCards(e.target.value)}
            />
          </div>

          {/* Progress feedback */}
          {progressLabel && (
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">{progressLabel}</p>
              {progressPct !== null && (
                <div className="h-1.5 w-full rounded-full bg-muted">
                  <div
                    className="h-1.5 rounded-full bg-primary transition-all"
                    style={{ width: `${progressPct}%` }}
                  />
                </div>
              )}
            </div>
          )}

          {/* Success */}
          {successMsg && (
            <p className="text-sm text-[var(--status-ok)]">{successMsg}</p>
          )}

          {/* Error with optional action_link */}
          {errorPayload && (
            <div className="text-sm text-destructive space-y-1">
              <p>{errorPayload.message}</p>
              {errorPayload.action_link && (
                <Link
                  to={errorPayload.action_link.href}
                  className="underline hover:opacity-80"
                  onClick={() => onOpenChange(false)}
                >
                  {errorPayload.action_link.label}
                </Link>
              )}
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => { onOpenChange(false); reset(); }}>
            {isDone ? 'Close' : 'Cancel'}
          </Button>
          {!isDone && (
            <Button
              onClick={() => { reset(); genMut.mutate(); }}
              disabled={!paperId || !deckId || isGenerating}
            >
              {isGenerating ? 'Generating…' : 'Generate'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

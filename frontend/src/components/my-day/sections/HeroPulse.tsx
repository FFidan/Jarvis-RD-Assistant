import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useNavigate, Link } from 'react-router-dom';
import { toast } from 'sonner';
import { Sparkles } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { ScoreStack } from './ScoreStack';
import { WhyChips } from '@/components/my-day/primitives/WhyChips';
import { ErrorSentinel } from '@/components/shared/ErrorSentinel';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchPulseToday } from '@/lib/api';
import { toScoreParts } from '@/lib/score-utils';
import { usePulseRating } from '@/hooks/usePulseRating';
import type { PulseDeck } from '@/types';

/**
 * Polished "no pulse yet" call-to-action. This is the hero card the README
 * screenshot captures, so the no-data state must read as an intentional
 * invitation — never a broken/red error panel. `fetchPulseToday` maps the
 * backend's no-data 404 to `null`, so `isError` only fires on genuine
 * failures; reaching here means there is simply no deck to triage today.
 */
function PulseEmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center gap-3 py-8 text-center">
      <Sparkles className="h-7 w-7 text-faint" aria-hidden />
      <p className="font-serif italic text-faint max-w-[34ch]">{message}</p>
      {/* Points at the surface that actually has the control: Generate lives
          on /pulse, and Papers has no Pulse affordance. */}
      <Button asChild size="sm" variant="outline">
        <Link to="/pulse">Open Pulse Deck</Link>
      </Button>
    </div>
  );
}

export function HeroPulse() {
  const navigate = useNavigate();
  const [currentIndex, setCurrentIndex] = useState(0);
  const lastDeckIdRef = useRef<number | null>(null);

  const { data: deck, isLoading, isError, error } = useQuery<PulseDeck | null>({
    queryKey: QUERY_KEYS.pulse.today(),
    queryFn: fetchPulseToday,
  });

  useEffect(() => {
    if (!deck) return;
    // Reset on regenerate (new deck_id)
    if (deck.deck_id !== lastDeckIdRef.current) {
      lastDeckIdRef.current = deck.deck_id;
      setCurrentIndex(0);
      return;
    }
    // Clamp if currentIndex meets or exceeds available cards (e.g. deck shrunk from refetch)
    if (currentIndex >= deck.cards.length) {
      setCurrentIndex(deck.cards.length);
    }
  }, [deck?.deck_id, deck?.cards.length, currentIndex]);

  const rateMutation = usePulseRating({
    onSuccess: () => setCurrentIndex((prev) => prev + 1),
  });

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-5 w-48" />
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-8 w-56" />
      </div>
    );
  }

  // `fetchPulseToday` swallows the no-data 404 to `null`, so `isError` only
  // fires for genuine failures (5xx / network). Reserve the calm error
  // sentinel for those — the empty/no-deck states render their own CTA below.
  if (isError) {
    return (
      <ErrorSentinel
        message={`Couldn't load your recommendations — ${error?.message ?? 'please try again'}.`}
      />
    );
  }

  const card = deck?.cards[currentIndex];

  // Cleared (rated all) — only when deck exists and we've advanced past last card
  if (deck && currentIndex >= deck.cards.length) {
    return (
      <PulseEmptyState message="All caught up — pulse cleared. Generate a fresh one from the Library." />
    );
  }

  if (!card) {
    return (
      <PulseEmptyState message="No Pulse for today yet — generate one from the Library." />
    );
  }

  const parts = toScoreParts(card.signals ?? {});
  const authorsLine = card.paper_authors?.join(', ') ?? '';

  const handleOpenAndFocus = () => {
    const store = usePomodoroStore.getState();
    if (store.phase !== 'idle') {
      toast.info(
        `Already focusing on "${store.attachedItem?.title ?? 'a task'}". Replace timer?`,
        {
          action: {
            label: 'Replace',
            onClick: () => {
              store.startWork({ id: card.paper_id, title: card.paper_title, type: 'paper' });
              navigate(`/paper/${card.paper_id}`);
            },
          },
        },
      );
      return;
    }
    navigate(`/paper/${card.paper_id}`);
    store.startWork({ id: card.paper_id, title: card.paper_title, type: 'paper' });
  };

  return (
    <div className="space-y-4">
      {/* Header: pill + meta */}
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center rounded-full bg-[var(--ink-blue,#0b3a8a)] px-2.5 py-0.5 text-[10px] font-mono font-semibold text-white">
          Next
        </span>
        <span className="font-mono text-[11px] text-faint">
          Triage today's pulse · ~6 min · #{currentIndex + 1} of {deck?.card_count ?? 1}
        </span>
      </div>

      {/* Title */}
      <h2
        className="font-serif text-[26px] leading-[1.18] tracking-tight max-w-[36ch] text-strong hover:text-[var(--ink-blue,#0b3a8a)] cursor-default transition-colors"
        onClick={handleOpenAndFocus}
      >
        {card.paper_title}
      </h2>

      {/* Authors */}
      {authorsLine && (
        <p className="font-mono text-[11px] text-meta truncate">{authorsLine}</p>
      )}

      {/* Reasoning / TL;DR */}
      <WhyChips signals={card.signals ?? {}} reasoning={card.reasoning} max={3} />

      {/* Score row */}
      <ScoreStack score={card.score} parts={parts} className="max-w-[28rem]" />

      {/* CTA buttons */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          size="sm"
          className="bg-[var(--ink-blue,#0b3a8a)] text-white hover:bg-[var(--ink-blue,#0b3a8a)]/90"
          onClick={handleOpenAndFocus}
        >
          Open &amp; start focus
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => rateMutation.mutate({ paperId: card.paper_id, rating: 'up' })}
          disabled={rateMutation.isPending}
        >
          Accept
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => rateMutation.mutate({ paperId: card.paper_id, rating: 'down' })}
          disabled={rateMutation.isPending}
        >
          Skip
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => rateMutation.mutate({ paperId: card.paper_id, rating: 'save' })}
          disabled={rateMutation.isPending}
        >
          Save for later
        </Button>

        {/* Keyboard hint */}
        <span className="ml-auto font-mono text-[10px] text-faint">
          ⏎ open · ⌥+a accept
        </span>
      </div>
    </div>
  );
}

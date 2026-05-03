import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { ScoreStack } from './ScoreStack';
import { WhyChips } from '@/components/my-day/primitives/WhyChips';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchPulseToday, ratePulseCard } from '@/lib/api';
import type { PulseDeck, PulseCardItem, PulseRating } from '@/types';

/** Extract 4-stop score parts from the flat signals map. */
function toScoreParts(signals: Record<string, number>) {
  return {
    emb: signals['embedding'] ?? signals['emb'] ?? 0,
    llm: signals['llm'] ?? signals['llm_relevance'] ?? 0,
    rec: signals['rec'] ?? signals['recommendation'] ?? 0,
    graph: signals['graph'] ?? signals['graph_boost'] ?? 0,
  };
}

export function HeroPulse() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [currentIndex, setCurrentIndex] = useState(0);

  const { data: deck, isLoading, isError, error } = useQuery<PulseDeck | null>({
    queryKey: ['pulse-today'],
    queryFn: fetchPulseToday,
  });

  const rateMutation = useMutation({
    mutationFn: ({ paperId, rating }: { paperId: number; rating: PulseRating }) =>
      ratePulseCard(paperId, rating),
    onSuccess: () => {
      setCurrentIndex((prev) => prev + 1);
    },
    onMutate: async ({ paperId, rating }) => {
      if (rating !== 'save') return undefined;
      const prev = queryClient.getQueryData<PulseDeck>(['pulse-today']);
      if (prev) {
        queryClient.setQueryData<PulseDeck>(['pulse-today'], {
          ...prev,
          cards: prev.cards.map((c: PulseCardItem) =>
            c.paper_id === paperId ? { ...c, user_state: 'to_read' } : c,
          ),
        });
      }
      return { prev };
    },
    onError: (err: Error, _vars, context) => {
      if (context?.prev !== undefined) {
        queryClient.setQueryData(['pulse-today'], context.prev);
      }
      toast.error(`Failed to rate card: ${err.message ?? 'unknown error'}`);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['pulse-today'] });
    },
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

  if (isError) {
    return (
      <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        Failed to load Pulse:{' '}
        {error instanceof Error ? error.message : 'unknown error'}
      </div>
    );
  }

  const card = deck?.cards[currentIndex];

  // Cleared (rated all) — only when deck exists and we've advanced past last card
  if (deck && currentIndex >= deck.cards.length) {
    return (
      <p className="text-faint italic font-serif text-center py-8">
        All caught up — pulse cleared. Generate a fresh one from the Research Feed.
      </p>
    );
  }

  if (!card) {
    return (
      <p className="text-faint italic font-serif text-center py-8">
        No Pulse for today yet — generate one from the Research Feed.
      </p>
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

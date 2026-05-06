import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { PulseCard } from '@/components/pulse/PulseCard';
import {
  fetchPulseToday,
  ratePulseCard,
} from '@/lib/api';
import { useJobStore } from '@/stores/job-store';
import type { PulseDeck as PulseDeckType, PulseRating, PulseSourceDiagnostic } from '@/types';

function sourceDiagnosticsFromStats(
  stats: Record<string, unknown>,
): Record<string, PulseSourceDiagnostic> {
  const raw = stats.source_diagnostics;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  return raw as Record<string, PulseSourceDiagnostic>;
}

/**
 * PulseDeck — renders today's Pulse deck header + grid of PulseCards.
 *
 * Used both in the My Day page (top-of-page widget) and in the Research
 * Feed "Today's Pulse" tab; TanStack Query dedupes the fetch so mounting
 * twice on one page is cheap.
 */
export function PulseDeck() {
  const navigate = useNavigate();
  const [ratedCards, setRatedCards] = useState<Set<number>>(new Set());

  const {
    data: deck,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery<PulseDeckType | null>({
    queryKey: ['pulse-today'],
    queryFn: fetchPulseToday,
  });

  const startJob = useJobStore((s) => s.startJob);
  const isGenerating = useJobStore((s) => s.hasRunning('pulse.generate'));
  const queryClient = useQueryClient();

  const rateMutation = useMutation({
    mutationFn: ({ paperId, rating }: { paperId: number; rating: PulseRating }) =>
      ratePulseCard(paperId, rating),
    onMutate: async ({ paperId, rating }) => {
      if (rating !== 'save') return undefined;
      const prev = queryClient.getQueryData<PulseDeckType>(['pulse-today']);
      if (prev) {
        queryClient.setQueryData<PulseDeckType>(['pulse-today'], {
          ...prev,
          cards: prev.cards.map((c) =>
            c.paper_id === paperId ? { ...c, user_state: 'to_read' } : c,
          ),
        });
      }
      return { prev };
    },
    onSuccess: (_data, { paperId }) => {
      setRatedCards((prev) => new Set(prev).add(paperId));
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

  const handleRate = (paperId: number, rating: PulseRating) => {
    rateMutation.mutate({ paperId, rating });
  };

  const handleOpen = (paperId: number) => {
    navigate(`/paper/${paperId}`);
  };

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <Card className="border-destructive/50 bg-destructive/5">
        <CardHeader className="pb-2">
          <CardTitle className="text-destructive text-base">
            Failed to load Pulse
          </CardTitle>
        </CardHeader>
        <CardContent className="flex items-center justify-between gap-4">
          <p className="text-muted-foreground text-sm">
            {error instanceof Error ? error.message : 'Unknown error'}
          </p>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (!deck) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-lg">
            <Sparkles className="h-5 w-5" />
            Your Pulse
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col items-start gap-3">
          <p className="text-muted-foreground text-sm">
            No Pulse deck yet today.
          </p>
          <Button
            size="sm"
            onClick={() => void startJob('pulse.generate', {})}
            disabled={isGenerating}
          >
            {isGenerating ? 'Generating...' : 'Generate now'}
          </Button>
          <p className="text-xs text-muted-foreground">
            Your daily AI-curated paper recommendations, personalised to your reading history and research interests.
          </p>
        </CardContent>
      </Card>
    );
  }

  const sourceDiagnostics = sourceDiagnosticsFromStats(deck.stats);
  const allDegradedDetails = Object.entries(sourceDiagnostics).filter(
    ([, diagnostic]) => diagnostic.status !== 'ok',
  );
  const degradedDetails = allDegradedDetails.slice(0, 3);
  const hiddenDegradedCount = Math.max(0, allDegradedDetails.length - degradedDetails.length);

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <Sparkles className="h-5 w-5" />
        <h2 className="text-lg font-semibold">
          Your Pulse — {deck.card_count} papers
        </h2>
        {deck.card_count === 0 && (
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            onClick={() => void startJob('pulse.generate', {})}
            disabled={isGenerating}
          >
            {isGenerating ? 'Generating...' : 'Regenerate'}
          </Button>
        )}
      </div>
      <p className="text-sm text-muted-foreground -mt-1">
        Your daily AI-curated paper recommendations, personalised to your reading history and research interests.
      </p>
      {deck.degraded_reason && (
        <div className="rounded-md border border-amber-400/60 bg-amber-500/10 px-3 py-2 text-sm text-[hsl(var(--badge-warn-fg))]">
          <div className="font-medium text-[hsl(var(--badge-warn-fg))]">{deck.degraded_reason}</div>
          {degradedDetails.length > 0 && (
            <div className="mt-1 space-y-0.5 text-xs text-[hsl(var(--badge-warn-fg))] opacity-80">
              {degradedDetails.map(([source, diagnostic]) => (
                <div key={source}>
                  <span className="font-medium">{source}</span>: {diagnostic.message}
                  {diagnostic.settings_hint ? ` ${diagnostic.settings_hint}` : ''}
                </div>
              ))}
              {hiddenDegradedCount > 0 && (
                <div className="font-medium">+{hiddenDegradedCount} more source warnings</div>
              )}
            </div>
          )}
        </div>
      )}
      <div className="space-y-3">
        {deck.cards.map((card) => (
          <PulseCard
            key={card.card_id}
            card={card}
            onRate={handleRate}
            onOpen={handleOpen}
            rated={ratedCards.has(card.paper_id)}
          />
        ))}
      </div>
    </section>
  );
}

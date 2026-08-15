import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Link, useNavigate } from 'react-router-dom';
import { Loader2, RefreshCw, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { PulseCard } from '@/components/pulse/PulseCard';
import { StaleBadge } from '@/components/pulse/StaleBadge';
import {
  fetchPulseToday,
  fetchPulseStats,
  fetchConfig,
  ApiError,
} from '@/lib/api';
import { useJobStore } from '@/stores/job-store';
import { errorMessage } from '@/lib/errors';
import { usePulseRating } from '@/hooks/usePulseRating';
import type { PulseDeck as PulseDeckType, PulseRating, PulseSourceDiagnostic, PulseStats } from '@/types';
import { LLM_SCORING_FAILED, suppressScoringFailed } from '@/components/pulse/reasoning-display';

function sourceDiagnosticsFromStats(
  stats: PulseDeckType['stats'],
): Record<string, PulseSourceDiagnostic> {
  return stats.source_diagnostics ?? {};
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
    queryKey: QUERY_KEYS.pulse.today(),
    queryFn: fetchPulseToday,
  });

  const { data: stats } = useQuery<PulseStats>({
    queryKey: QUERY_KEYS.pulse.statsAll(),
    queryFn: () => fetchPulseStats(),
  });
  const { data: configs, isLoading: configLoading } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });
  const pulseTurnedOff = configs?.some(
    (config) => config.key === 'pulse.enabled' && config.value === false,
  );

  const startJob = useJobStore((s) => s.startJob);
  const isGenerating = useJobStore((s) => s.hasRunning('pulse.generate'));

  const rateMutation = usePulseRating({
    onSuccess: ({ paperId }) => setRatedCards((prev) => new Set(prev).add(paperId)),
  });

  const handleRate = (paperId: number, rating: PulseRating) => {
    rateMutation.mutate({ paperId, rating });
  };

  const handleOpen = (paperId: number) => {
    navigate(`/paper/${paperId}`);
  };

  if (isLoading || configLoading) {
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
            Couldn't load your recommendations
          </CardTitle>
        </CardHeader>
        <CardContent className="flex items-center justify-between gap-4">
          <p className="text-muted-foreground text-sm">
            {errorMessage(error)}
          </p>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const handleGenerateNow = () => {
    startJob('pulse.generate', {}).catch((err: unknown) => {
      if (err instanceof ApiError && err.status === 409) {
        toast.info('Pulse is already running. Your deck will be ready shortly.');
      } else if (err instanceof ApiError && err.status === 429) {
        toast.error("You've refreshed too many times — try again shortly.");
      } else {
        toast.error("Couldn't refresh your recommendations.");
      }
    });
  };

  if (pulseTurnedOff && (!deck || deck.cards.length === 0)) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-lg">Your Pulse</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col items-start gap-3">
          <p className="text-sm text-muted-foreground">Pulse is turned off in Settings.</p>
          <Button asChild size="sm" variant="outline">
            <Link to="/settings?section=system&item=pulse">Open Settings</Link>
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
            data-tour-id="pulse-generate-btn"
            size="sm"
            onClick={handleGenerateNow}
            disabled={isGenerating}
          >
            {isGenerating ? 'Generating...' : 'Generate now'}
          </Button>
          <p className="text-xs text-muted-foreground">
            Your daily AI-curated paper recommendations, personalized to your reading history and research interests.
          </p>
        </CardContent>
      </Card>
    );
  }

  if (deck.empty_reason === 'no_data_yet') {
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
            No papers have been ingested yet. Add some sources or upload papers to get started.
          </p>
          <Button
            data-tour-id="pulse-generate-btn"
            size="sm"
            onClick={handleGenerateNow}
            disabled={isGenerating}
          >
            {isGenerating ? 'Generating...' : 'Generate now'}
          </Button>
          <p className="text-xs text-muted-foreground">
            Once you have papers in your library, Pulse will rank and curate a personalized daily deck.
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

  const allScoringUnavailable =
    deck.cards.length > 0 &&
    deck.cards.every((c) => c.reasoning === LLM_SCORING_FAILED);

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <Sparkles className="h-5 w-5" />
        <h2 className="text-lg font-semibold">
          Your Pulse — {deck.card_count} papers
        </h2>
        {deck.is_stale && typeof deck.stale_age_days === 'number' && (
          <StaleBadge
            ageDays={deck.stale_age_days}
            diagnostics={deck.stale_diagnostics ?? null}
            onRetry={handleGenerateNow}
          />
        )}
        <Button
          size="sm"
          variant="outline"
          className="ml-auto"
          onClick={handleGenerateNow}
          disabled={isGenerating}
        >
          {isGenerating ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Generating…
            </>
          ) : (
            <>
              <RefreshCw className="h-3.5 w-3.5" />
              Regenerate
            </>
          )}
        </Button>
      </div>
      <p className="text-sm text-muted-foreground -mt-1">
        Your daily AI-curated paper recommendations, personalized to your reading history and research interests.
      </p>
      {stats?.has_learned_model === false && (
        <p className="text-xs text-muted-foreground">
          Basic ranking (learning from your feedback)
        </p>
      )}
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
      {allScoringUnavailable && !deck.degraded_reason && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-muted bg-muted/20 px-3 py-2">
          <p className="text-sm text-muted-foreground">
            AI scoring is unavailable for all cards. Regenerating may improve results.
          </p>
          <Button
            size="sm"
            variant="outline"
            onClick={handleGenerateNow}
            disabled={isGenerating}
          >
            {isGenerating ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Generating…
              </>
            ) : (
              'Regenerate'
            )}
          </Button>
        </div>
      )}
      {deck.cards.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-start gap-3 py-6">
            <p className="text-muted-foreground text-sm">
              Today's deck has no cards yet — regenerate to refresh your recommendations.
            </p>
            <Button size="sm" onClick={handleGenerateNow} disabled={isGenerating}>
              {isGenerating ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Generating…
                </>
              ) : (
                <>
                  <RefreshCw className="h-3.5 w-3.5" />
                  Regenerate deck
                </>
              )}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {deck.cards.map((card, idx) => {
            const pendingSavePaperId = rateMutation.variables?.paperId;
            const displayCard = deck.degraded_reason
              ? { ...card, reasoning: suppressScoringFailed(card.reasoning) }
              : card;
            return (
              <div
                key={card.card_id}
                data-tour-id={idx === 0 ? 'pulse-card-first' : undefined}
              >
                <PulseCard
                  card={displayCard}
                  onRate={handleRate}
                  onOpen={handleOpen}
                  rated={ratedCards.has(card.paper_id)}
                  savePending={pendingSavePaperId === card.paper_id && rateMutation.isPending}
                  degraded={!!deck.degraded_reason}
                />
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

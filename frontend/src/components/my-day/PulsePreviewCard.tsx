import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Sparkles, RefreshCw, Loader2, AlertTriangle } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { PulseCard } from '@/components/pulse/PulseCard';
import { useJobStore } from '@/stores/job-store';
import { ApiError, fetchPulseToday, ratePulseCard } from '@/lib/api';
import type { PulseDeck, PulseRating } from '@/types';

/** Format a date as "HH:MM" (24h). */
function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Returns the age in hours since `iso`. */
function hoursAgo(iso: string): number {
  return (Date.now() - new Date(iso).getTime()) / (1000 * 60 * 60);
}

/** Next auto-run estimate: next 6 AM. */
function nextAutoRun(): string {
  const d = new Date();
  d.setDate(d.getDate() + (d.getHours() >= 6 ? 1 : 0));
  d.setHours(6, 0, 0, 0);
  return formatTime(d.toISOString());
}

interface GenerateButtonProps {
  deck: PulseDeck | null;
  isGenerating: boolean;
  isFetching: boolean;
  onGenerate: () => void;
  onRefetch: () => void;
}

function GenerateButton({
  deck,
  isGenerating,
  isFetching,
  onGenerate,
  onRefetch,
}: GenerateButtonProps) {
  if (isGenerating) {
    return (
      <Button size="sm" disabled>
        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
        Generating…
      </Button>
    );
  }

  if (!deck) {
    return (
      <Button size="sm" onClick={onGenerate}>
        Generate Pulse now
      </Button>
    );
  }

  const age = hoursAgo(deck.generated_at);
  if (age < 1) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">
          Pulse up to date · next auto-run at {nextAutoRun()}
        </span>
        {/* B.6 — Radix Tooltip + spin-on-isFetching for the refresh icon button */}
        <TooltipProvider delayDuration={150}>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 w-7 p-0"
                onClick={onRefetch}
                aria-label="Re-fetch the latest deck"
                disabled={isFetching}
              >
                <RefreshCw className={`h-3.5 w-3.5${isFetching ? ' animate-spin' : ''}`} />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Re-fetch the latest deck
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>
    );
  }

  return (
    <Button size="sm" variant="outline" onClick={onGenerate}>
      <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
      Refresh Pulse
    </Button>
  );
}

interface PulsePreviewCardProps {
  containerRef?: React.RefObject<HTMLDivElement | null>;
}

export function PulsePreviewCard({ containerRef }: PulsePreviewCardProps) {
  const navigate = useNavigate();
  const [ratedCards, setRatedCards] = useState<Set<number>>(new Set());

  const {
    data: deck,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
  } = useQuery<PulseDeck | null>({
    queryKey: QUERY_KEYS.pulse.today(),
    queryFn: fetchPulseToday,
  });

  const startJob = useJobStore((s) => s.startJob);
  const isGenerating = useJobStore((s) => s.hasRunning('pulse.generate'));
  const queryClient = useQueryClient();

  const handleGenerate = async () => {
    try {
      await startJob('pulse.generate', {});
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        toast.error('Rate limited — you can generate up to 3 Pulse decks per hour.');
      } else {
        toast.error('Failed to start Pulse generation');
      }
    }
  };

  const rateMutation = useMutation({
    mutationFn: ({ paperId, rating }: { paperId: number; rating: PulseRating }) =>
      ratePulseCard(paperId, rating),
    onMutate: async ({ paperId, rating }) => {
      if (rating !== 'save') return undefined;
      const prev = queryClient.getQueryData<PulseDeck>(QUERY_KEYS.pulse.today());
      if (prev) {
        queryClient.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), {
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
        queryClient.setQueryData(QUERY_KEYS.pulse.today(), context.prev);
      }
      toast.error(`Failed to rate card: ${err.message ?? 'unknown error'}`);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.pulse.today() });
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
      <div ref={containerRef} className="space-y-3">
        <Skeleton className="h-6 w-56" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <Card ref={containerRef} className="border-destructive/50 bg-destructive/5">
        <CardHeader className="pb-2">
          <CardTitle className="text-destructive text-base">Failed to load Pulse</CardTitle>
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

  const previewCards = deck?.cards.slice(0, 3) ?? [];
  const totalCount = deck?.card_count ?? 0;

  return (
    <Card ref={containerRef}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <CardTitle className="flex items-center gap-2 text-lg">
            <Sparkles className="h-5 w-5" />
            {deck
              ? `Today's Pulse — ${totalCount} paper${totalCount !== 1 ? 's' : ''}`
              : "Today's Pulse"}
          </CardTitle>
          <GenerateButton
            deck={deck ?? null}
            isGenerating={isGenerating}
            isFetching={isFetching}
            onGenerate={handleGenerate}
            onRefetch={async () => {
              await refetch();
              toast.success('Pulse refreshed', { duration: 1500 });
            }}
          />
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Empty state: no deck ever generated */}
        {!deck && (
          <div className="flex flex-col items-center gap-3 py-6 text-center">
            <Sparkles className="h-8 w-8 text-muted-foreground" />
            <div>
              <p className="font-medium">Generate your first Pulse</p>
              <p className="text-xs text-muted-foreground mt-1 max-w-xs">
                Your daily AI-curated paper recommendations, personalised to your reading history and research interests.
              </p>
            </div>
          </div>
        )}

        {/* Degraded warning banner */}
        {deck && deck.degraded_reason && (
          <div className="flex items-start gap-2 rounded-md bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
            <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
            <span>{deck.degraded_reason}</span>
          </div>
        )}

        {/* Top 3 preview cards — Trash+Reject hidden per spec §5.2 (Preview shows 👍/👎/💾 only) */}
        {previewCards.map((card) => (
          <PulseCard
            key={card.card_id}
            card={card}
            onRate={handleRate}
            onOpen={handleOpen}
            rated={ratedCards.has(card.paper_id)}
            hideTrashAndReject
            savePending={rateMutation.isPending}
          />
        ))}

        {/* "View all" link */}
        {deck && totalCount > 0 && (
          <div className="pt-1 text-right">
            <Link
              to="/pulse"
              className="text-sm text-primary hover:underline"
            >
              View all {totalCount} →
            </Link>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

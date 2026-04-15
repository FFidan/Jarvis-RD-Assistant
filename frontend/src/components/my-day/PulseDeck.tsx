import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { PulseCard } from '@/components/pulse/PulseCard';
import {
  fetchPulseToday,
  generatePulseNow,
  ratePulseCard,
} from '@/lib/api';
import type { PulseDeck as PulseDeckType, PulseRating } from '@/types';

/**
 * PulseDeck — renders today's Pulse deck header + grid of PulseCards.
 *
 * Used both in the My Day page (top-of-page widget) and in the Research
 * Feed "Today's Pulse" tab; TanStack Query dedupes the fetch so mounting
 * twice on one page is cheap.
 */
export function PulseDeck() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
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

  const generateMutation = useMutation({
    mutationFn: generatePulseNow,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['pulse-today'] });
      queryClient.invalidateQueries({ queryKey: ['pulse-history'] });
    },
  });

  const rateMutation = useMutation({
    mutationFn: ({ paperId, rating }: { paperId: number; rating: PulseRating }) =>
      ratePulseCard(paperId, rating),
    onSuccess: (_data, { paperId }) => {
      setRatedCards((prev) => new Set(prev).add(paperId));
    },
    onError: (err) => {
      // TODO(stream-I): replace with useToast once a toast hook lands.
      // eslint-disable-next-line no-console
      console.error('Pulse rating failed', err);
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
            onClick={() => generateMutation.mutate()}
            disabled={generateMutation.isPending}
          >
            {generateMutation.isPending ? 'Generating...' : 'Generate now'}
          </Button>
          {generateMutation.isError && (
            <p className="text-destructive text-xs">
              Generation failed. Please retry.
            </p>
          )}
        </CardContent>
      </Card>
    );
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <Sparkles className="h-5 w-5" />
        <h2 className="text-lg font-semibold">
          Your Pulse — {deck.card_count} papers
        </h2>
      </div>
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

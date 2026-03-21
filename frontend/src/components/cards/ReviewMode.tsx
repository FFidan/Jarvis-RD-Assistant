import { useState, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RotateCcw, Eye, CheckCircle } from 'lucide-react';
import type { Card as CardType } from '@/types';
import { getNextReview, submitReview } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { EmptyState } from '@/components/EmptyState';

const RATING_LABELS: Record<number, { label: string; color: string }> = {
  1: { label: 'Again', color: 'bg-red-500 hover:bg-red-600' },
  2: { label: 'Hard', color: 'bg-orange-500 hover:bg-orange-600' },
  3: { label: 'Good', color: 'bg-blue-500 hover:bg-blue-600' },
  4: { label: 'Easy', color: 'bg-green-500 hover:bg-green-600' },
};

export function ReviewMode() {
  const queryClient = useQueryClient();
  const [revealed, setRevealed] = useState(false);
  const startTime = useRef<number>(Date.now());

  const { data: cards = [], isLoading, refetch } = useQuery({
    queryKey: ['review-next'],
    queryFn: () => getNextReview(1),
  });

  const reviewMut = useMutation({
    mutationFn: ({ cardId, rating }: { cardId: number; rating: number }) => {
      const duration = Date.now() - startTime.current;
      return submitReview(cardId, rating, duration);
    },
    onSuccess: () => {
      setRevealed(false);
      startTime.current = Date.now();
      queryClient.invalidateQueries({ queryKey: ['card-stats'] });
      refetch();
    },
  });

  const currentCard: CardType | null = cards.length > 0 ? cards[0] : null;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <p className="text-muted-foreground">Loading next card...</p>
      </div>
    );
  }

  if (!currentCard) {
    return (
      <EmptyState
        title="No cards to review"
        description="All caught up! Generate cards from a paper or wait for scheduled reviews."
        icon={CheckCircle}
      />
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {/* Flashcard */}
      <div
        className="relative cursor-pointer perspective-1000"
        onClick={() => !revealed && setRevealed(true)}
      >
        <Card className="min-h-[280px] transition-all duration-300">
          <CardContent className="flex flex-col items-center justify-center p-8 min-h-[280px]">
            {!revealed ? (
              <>
                <Badge variant="outline" className="mb-4 capitalize">
                  {currentCard.card_type}
                </Badge>
                <p className="text-lg text-center font-medium leading-relaxed">
                  {currentCard.front}
                </p>
                <div className="mt-6 flex items-center gap-2 text-muted-foreground">
                  <Eye className="h-4 w-4" />
                  <span className="text-sm">Click to reveal answer</span>
                </div>
              </>
            ) : (
              <>
                <Badge variant="outline" className="mb-2 capitalize">
                  {currentCard.card_type}
                </Badge>
                <p className="text-sm text-muted-foreground text-center mb-4">
                  {currentCard.front}
                </p>
                <div className="w-full border-t my-2" />
                <p className="text-lg text-center font-medium leading-relaxed mt-4">
                  {currentCard.back}
                </p>
                {currentCard.evidence?.quote && (
                  <blockquote className="mt-4 border-l-2 pl-4 text-sm text-muted-foreground italic">
                    {currentCard.evidence.quote}
                    {currentCard.evidence.page_number && (
                      <span className="not-italic"> (p. {currentCard.evidence.page_number})</span>
                    )}
                  </blockquote>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Rating buttons */}
      {revealed && (
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-muted-foreground">How well did you know this?</p>
          <div className="flex gap-3">
            {Object.entries(RATING_LABELS).map(([rating, { label, color }]) => (
              <Button
                key={rating}
                onClick={() =>
                  reviewMut.mutate({
                    cardId: currentCard.id,
                    rating: Number(rating),
                  })
                }
                disabled={reviewMut.isPending}
                className={`${color} text-white min-w-[80px]`}
              >
                {label}
              </Button>
            ))}
          </div>
        </div>
      )}

      {/* Skip / shuffle */}
      {!revealed && (
        <div className="flex justify-center">
          <Button variant="ghost" size="sm" onClick={() => refetch()}>
            <RotateCcw className="mr-1 h-4 w-4" /> Skip
          </Button>
        </div>
      )}
    </div>
  );
}

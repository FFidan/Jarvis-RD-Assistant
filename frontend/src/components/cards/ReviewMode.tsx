/**
 * ReviewMode — focused review session card canvas.
 *
 * Renders the full-width serif card layout per the v5 Learning Cards IA redesign:
 * eyebrow strip (§ CARD n · DECK NAME | last seen N days) → question text → reveal
 * separator → § ANSWER section → rating row.
 *
 * P2 OFFLINE SEAM (Wave 3 / functional-track):
 * The "submit review" side-effect is intentionally isolated behind the single
 * `submitReviewFn` prop. To add offline IndexedDB outbox + sync in Wave 3:
 *   1. Create a `useOfflineReviewQueue` hook that writes to IndexedDB and returns
 *      a wrapped submit function.
 *   2. Pass it as `submitReviewFn` from `LearningCardsPage` — zero changes needed
 *      here or in `SessionShell`.
 * The online implementation (`submitReview` from @/lib/api) is the default.
 */

import { useState, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RotateCcw } from 'lucide-react';
import type { Card as CardType } from '@/types';
import { getNextReview, submitReview, fetchDecks } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { EvidenceSnapshot } from '@/components/shared/EvidenceSnapshot';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import { computeLastSeenDays, resolveDeckName } from '@/components/cards/SessionShell';

const RATING_LABELS: Record<number, { label: string; color: string }> = {
  1: { label: 'Again', color: 'bg-red-500 hover:bg-red-600 text-white' },
  2: { label: 'Hard', color: 'bg-orange-500 hover:bg-orange-600 text-white' },
  3: { label: 'Good', color: 'bg-blue-500 hover:bg-blue-600 text-white' },
  4: { label: 'Easy', color: 'bg-green-500 hover:bg-green-600 text-white' },
};

export interface ReviewModeProps {
  /** Index of the current card in the session (1-based, for eyebrow display). */
  sessionCardIndex: number;
  /** Deck filter: when non-null, fetches only cards from this deck. */
  deckId?: number | null;
  /**
   * P2 OFFLINE SEAM — isolated submit boundary.
   * Default: online `submitReview` from @/lib/api.
   * Wave 3: replace with an offline-aware function that writes to IndexedDB outbox
   * and advances local FSRS state optimistically. Signature is identical to the
   * online variant so swap is a one-liner at the call site.
   */
  submitReviewFn?: (cardId: number, rating: number, durationMs: number) => Promise<unknown>;
  /** Called after a successful review submission (used to advance session progress). */
  onReviewSuccess?: () => void;
  /** Called when the queue is exhausted (triggers session-complete panel). */
  onSessionEnd?: () => void;
}

export function ReviewMode({
  sessionCardIndex,
  deckId = null,
  submitReviewFn = submitReview,
  onReviewSuccess,
  onSessionEnd,
}: ReviewModeProps) {
  const queryClient = useQueryClient();
  const [revealed, setRevealed] = useState(false);
  const startTime = useRef<number>(Date.now());

  const reviewQueryKey = deckId != null
    ? ['review-next', { deckId }]
    : ['review-next'];

  const { data: cards = [], isLoading, isError, refetch } = useQuery({
    queryKey: reviewQueryKey,
    queryFn: () => getNextReview(1, deckId ?? undefined),
  });

  // Pre-fetch decks to resolve deck name for the eyebrow.
  const { data: decks = [] } = useQuery({
    queryKey: ['decks'],
    queryFn: fetchDecks,
  });

  // P2 OFFLINE SEAM: this mutation wraps the isolated `submitReviewFn` prop.
  // To go offline, swap submitReviewFn without touching the mutation logic here.
  const reviewMut = useMutation({
    mutationFn: ({ cardId, rating }: { cardId: number; rating: number }) => {
      const duration = Date.now() - startTime.current;
      return submitReviewFn(cardId, rating, duration);
    },
    onSuccess: () => {
      setRevealed(false);
      startTime.current = Date.now();
      queryClient.invalidateQueries({ queryKey: ['card-stats'] });
      onReviewSuccess?.();
      refetch().then(({ data }) => {
        if (!data || (data as CardType[]).length === 0) {
          onSessionEnd?.();
        }
      });
    },
  });

  const currentCard: CardType | null = cards.length > 0 ? (cards[0] as CardType) : null;
  const deckName = currentCard ? resolveDeckName(decks, currentCard.deck_id) : null;
  const lastSeenDays = currentCard ? computeLastSeenDays(currentCard.updated_at) : null;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <p className="text-muted-foreground text-sm">Loading next card…</p>
      </div>
    );
  }

  if (isError) {
    return <QueryErrorState onRetry={refetch} />;
  }

  if (!currentCard) {
    // Queue exhausted — signal parent to show session-complete panel.
    // Render nothing here; parent handles the transition via onSessionEnd.
    return null;
  }

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      {/* Eyebrow strip */}
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] font-semibold tracking-widest text-muted-foreground uppercase">
          § Card {sessionCardIndex}{deckName ? ` · ${deckName.toUpperCase()}` : ''}
        </span>
        {lastSeenDays !== null && (
          <span className="text-[11px] text-muted-foreground">
            last seen {lastSeenDays === 0 ? 'today' : `${lastSeenDays}d`}
          </span>
        )}
      </div>

      {/* Card canvas — paper surface, no shadow box */}
      <div
        className="cursor-pointer"
        onClick={() => !revealed && setRevealed(true)}
        role="button"
        tabIndex={0}
        aria-label="Reveal answer"
        onKeyDown={(e) => e.key === 'Enter' && !revealed && setRevealed(true)}
      >
        {/* Question */}
        <p className="text-3xl font-serif leading-snug text-strong">
          {currentCard.front}
        </p>

        {!revealed && (
          <p className="mt-4 text-sm text-muted-foreground">
            Click to reveal answer
          </p>
        )}

        {/* Answer section — revealed */}
        {revealed && (
          <div className="mt-6 space-y-4">
            <hr className="border-hair" />

            <p className="text-[10px] font-semibold tracking-widest text-[hsl(var(--ring))] uppercase">
              § Answer
            </p>

            <p className="text-base leading-relaxed text-foreground">
              {currentCard.back}
            </p>

            {currentCard.evidence?.quote && (
              <blockquote className="border-l-2 pl-4 text-sm text-muted-foreground italic">
                {currentCard.evidence.quote}
                {currentCard.evidence.page_number && (
                  <span className="not-italic"> (p. {currentCard.evidence.page_number})</span>
                )}
              </blockquote>
            )}

            {currentCard.evidence?.snapshot_path &&
              currentCard.paper_id != null &&
              currentCard.evidence.page_number != null && (
                <div className="flex justify-center">
                  <EvidenceSnapshot
                    paperId={currentCard.paper_id}
                    page={currentCard.evidence.page_number}
                    altText={`Page ${currentCard.evidence.page_number} snapshot`}
                    variant="thumbnail"
                  />
                </div>
              )}
          </div>
        )}
      </div>

      {/* Rating row — only after reveal */}
      {revealed && (
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-muted-foreground">How well did you know this?</p>
          <div className="flex gap-3">
            {Object.entries(RATING_LABELS).map(([rating, { label, color }]) => (
              <Button
                key={rating}
                onClick={() =>
                  reviewMut.mutate({ cardId: currentCard.id, rating: Number(rating) })
                }
                disabled={reviewMut.isPending}
                className={`${color} min-w-[80px]`}
              >
                {label}
              </Button>
            ))}
          </div>
        </div>
      )}

      {/* Skip — only when unrevealed */}
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

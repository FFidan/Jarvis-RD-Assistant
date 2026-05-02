import * as React from 'react';
import { Trash2, ThumbsDown, Bookmark, HelpCircle, CheckCircle, AlertTriangle } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { WhyPopover } from '@/components/pulse/WhyPopover';
import { FeedbackButtons } from '@/components/shared/FeedbackButtons';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import { trashAndRejectPaper, unsavePaper } from '@/lib/api';
import type { PulseCardItem, PulseRating } from '@/types';

export interface PulseCardProps {
  card: PulseCardItem;
  onRate: (paperId: number, rating: PulseRating) => void;
  onOpen?: (paperId: number) => void;
  rated?: boolean;
  /**
   * Hide the 🗑+👎 (Trash & Reject) action — used by the My Day Pulse Preview
   * (top-3 widget) per spec §5.2 lines 345-346 which differentiates the full
   * Pulse Deck card (👍/👎/💾/🗑+👎) from the My Day Pulse Preview (👍/👎/💾).
   * The full /pulse Pulse Deck page leaves this default (false) → all 4 actions.
   */
  hideTrashAndReject?: boolean;
}

/**
 * Single Pulse deck card — rank badge, title, authors, reasoning preview,
 * and rate / save / why actions.
 *
 * Visually mirrors the Research Feed paper cards (`rounded-lg border p-4`
 * layout with a primary content column and an action rail). Clicking the
 * card body (outside the action buttons) calls `onOpen(paper_id)`.
 */
export function PulseCard({
  card,
  onRate,
  onOpen,
  rated = false,
  hideTrashAndReject = false,
}: PulseCardProps) {
  const queryClient = useQueryClient();

  const authorsDisplay = React.useMemo(() => {
    const first = card.paper_authors.slice(0, 3).join(', ');
    return card.paper_authors.length > 3 ? `${first}, ...` : first;
  }, [card.paper_authors]);

  const handleBodyClick = () => {
    if (onOpen) onOpen(card.paper_id);
  };

  const stop = (e: React.MouseEvent) => e.stopPropagation();

  const trashAndRejectMut = useMutation({
    mutationFn: () => trashAndRejectPaper(card.paper_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['pulse-deck'] });
      queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
      queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
      toast.success('Trashed and excluded similar topics');
    },
    onError: (err) =>
      toast.error('Failed to trash & reject', {
        description: err instanceof Error ? err.message : 'Unknown error',
      }),
  });

  const unsaveMut = useMutation({
    mutationFn: () => unsavePaper(card.paper_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['pulse-today'] });
      toast.success('Paper moved back to Inbox');
    },
    onError: (err) =>
      toast.error('Failed to unsave paper', {
        description: err instanceof Error ? err.message : 'Unknown error',
      }),
  });

  // Save button is "active" (bookmark filled) when the paper is already saved (to_read)
  const isSaved = rated && card.user_state === 'to_read';

  const handleSaveClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isSaved) {
      unsaveMut.mutate();
    } else {
      onRate(card.paper_id, 'save');
    }
  };

  return (
    <div
      data-testid="pulse-card"
      className={cn(
        'rounded-lg border p-4 transition-colors',
        onOpen && 'cursor-pointer hover:bg-muted/30',
      )}
      onClick={handleBodyClick}
    >
      <div className="flex gap-4">
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex items-center gap-2">
            <Badge variant="secondary" className="font-mono text-xs">
              #{card.rank}
            </Badge>
            <InfoTooltip
              content={`Composite score: ${card.score.toFixed(2)}`}
            />
            {card.paper_url && (
              <a
                href={card.paper_url}
                target="_blank"
                rel="noreferrer"
                onClick={stop}
                className="text-[11px] text-muted-foreground underline-offset-2 hover:underline"
              >
                source
              </a>
            )}
          </div>
          <h3 className="text-lg font-semibold leading-tight">
            {card.paper_title}
          </h3>
          <p className="mt-1 text-sm text-muted-foreground">{authorsDisplay}</p>
          {card.reasoning && (
            <div className="mt-2 flex items-start gap-1.5">
              <p className="line-clamp-2 text-sm italic text-muted-foreground">
                {card.reasoning}
              </p>
              {card.reasoning_verified === true && (
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <CheckCircle
                        data-testid="reasoning-verified-icon"
                        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-green-600"
                        aria-label="Reasoning verified"
                      />
                    </TooltipTrigger>
                    <TooltipContent side="top" className="max-w-xs text-xs">
                      Reasoning verified against paper title/abstract
                      {card.reasoning_confidence && ` (${card.reasoning_confidence})`}
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              )}
              {card.reasoning_verified === false && (
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <AlertTriangle
                        data-testid="reasoning-unverified-icon"
                        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500"
                        aria-label="Reasoning not verified"
                      />
                    </TooltipTrigger>
                    <TooltipContent side="top" className="max-w-xs text-xs">
                      Reasoning not verified against paper title/abstract
                      {card.reasoning_confidence && ` (${card.reasoning_confidence})`}
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              )}
            </div>
          )}
        </div>

        <div
          className="flex shrink-0 flex-col items-end gap-2"
          onClick={stop}
        >
          <div className="flex gap-1">
            {/* Pulse-deck cards are always discovery_origin='pulse' by definition (spec §5.2).
                PulseCardResponse model does not surface the field; we hardcode it for the
                FeedbackButtons gate. */}
            <FeedbackButtons
              paperId={card.paper_id}
              discoveryOrigin="pulse"
              source="pulse_thumbs"
              recentFeedback={null}
              size="sm"
            />
            <Button
              variant={isSaved ? 'default' : 'outline'}
              size="sm"
              aria-label={isSaved ? 'Unsave' : 'Save'}
              disabled={unsaveMut.isPending}
              onClick={handleSaveClick}
            >
              <Bookmark className="h-3.5 w-3.5" />
            </Button>
            {!hideTrashAndReject && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={trashAndRejectMut.isPending}
                onClick={() => trashAndRejectMut.mutate()}
                title="Trash and don't recommend similar"
                aria-label="Trash and reject"
              >
                <Trash2 size={14} />
                <ThumbsDown size={12} className="ml-0.5 -mr-0.5" />
              </Button>
            )}
          </div>
          <WhyPopover
            cardId={card.card_id}
            trigger={
              <Button variant="ghost" size="sm" aria-label="Why?">
                <HelpCircle className="mr-1 h-3.5 w-3.5" />
                Why?
              </Button>
            }
          />
        </div>
      </div>
    </div>
  );
}

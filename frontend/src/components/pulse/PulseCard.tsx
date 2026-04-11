import * as React from 'react';
import { ThumbsUp, ThumbsDown, Bookmark, HelpCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { WhyPopover } from '@/components/pulse/WhyPopover';
import { cn } from '@/lib/utils';
import type { PulseCardItem, PulseRating } from '@/types';

export interface PulseCardProps {
  card: PulseCardItem;
  onRate: (paperId: number, rating: PulseRating) => void;
  onOpen?: (paperId: number) => void;
}

/**
 * Single Pulse deck card — rank badge, title, authors, reasoning preview,
 * and rate / save / why actions.
 *
 * Visually mirrors the Research Feed paper cards (`rounded-lg border p-4`
 * layout with a primary content column and an action rail). Clicking the
 * card body (outside the action buttons) calls `onOpen(paper_id)`.
 */
export function PulseCard({ card, onRate, onOpen }: PulseCardProps) {
  const authorsDisplay = React.useMemo(() => {
    const first = card.paper_authors.slice(0, 3).join(', ');
    return card.paper_authors.length > 3 ? `${first}, ...` : first;
  }, [card.paper_authors]);

  const handleBodyClick = () => {
    if (onOpen) onOpen(card.paper_id);
  };

  const stop = (e: React.MouseEvent) => e.stopPropagation();

  return (
    <div
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
            <p className="mt-2 line-clamp-2 text-sm italic text-muted-foreground">
              {card.reasoning}
            </p>
          )}
        </div>

        <div
          className="flex shrink-0 flex-col items-end gap-2"
          onClick={stop}
        >
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="sm"
              aria-label="Thumbs up"
              onClick={(e) => {
                e.stopPropagation();
                onRate(card.paper_id, 'up');
              }}
            >
              <ThumbsUp className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              aria-label="Thumbs down"
              onClick={(e) => {
                e.stopPropagation();
                onRate(card.paper_id, 'down');
              }}
            >
              <ThumbsDown className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              aria-label="Save"
              onClick={(e) => {
                e.stopPropagation();
                onRate(card.paper_id, 'save');
              }}
            >
              <Bookmark className="h-3.5 w-3.5" />
            </Button>
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

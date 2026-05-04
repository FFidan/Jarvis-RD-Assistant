import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ThumbsUp, ThumbsDown, Bookmark } from 'lucide-react';
import { toast } from 'sonner';
import { ratePulseCard } from '@/lib/api';
import type { PulseCardItem, PulseRating } from '@/types';
import { ScoreStack } from './ScoreStack';
import { HashtagChips } from '@/components/my-day/primitives/HashtagChips';
import { toScoreParts } from '@/lib/score-utils';

export interface PulseRowProps {
  card: PulseCardItem;
  rank: number;
}

export function PulseRow({ card, rank }: PulseRowProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const rateMutation = useMutation({
    mutationFn: ({ paperId, rating }: { paperId: number; rating: PulseRating }) =>
      ratePulseCard(paperId, rating),
    onError: (err: Error) => {
      toast.error(`Failed to rate card: ${err.message ?? 'unknown error'}`);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['pulse-today'] });
    },
  });

  const handleRate = (rating: PulseRating) => {
    rateMutation.mutate({ paperId: card.paper_id, rating });
  };

  const authorsLine = card.paper_authors?.join(', ') ?? '';
  const parts = toScoreParts(card.signals ?? {});

  return (
    <div className="group grid grid-cols-[28px_1fr_auto] gap-4 py-3 border-b border-hair last:border-b-0">
      {/* Column 1: rank */}
      <div className="pt-0.5">
        <span className="font-mono text-[10px] text-faint tabular-nums">
          #{rank}
        </span>
      </div>

      {/* Column 2: body */}
      <div className="min-w-0 space-y-1">
        <button
          className="font-serif text-[16.5px] leading-snug tracking-tight text-strong hover:text-[var(--ink-blue,#0b3a8a)] text-left transition-colors"
          onClick={() => navigate(`/paper/${card.paper_id}`)}
        >
          {card.paper_title}
        </button>

        {authorsLine && (
          <p className="font-mono text-[10px] text-meta truncate">
            {authorsLine}
          </p>
        )}

        {card.reasoning && (
          <p className="text-[12.5px] text-soft mt-1 line-clamp-2 leading-relaxed">
            {card.reasoning}
          </p>
        )}

        <div className="pt-1">
          <ScoreStack score={card.score} parts={parts} className="max-w-[22rem]" showBadges={false} />
        </div>
        <HashtagChips tags={card.tags ?? null} />
      </div>

      {/* Column 3: action buttons */}
      <div className="flex flex-col gap-1 opacity-50 group-hover:opacity-100 transition-opacity">
        <button
          aria-label="Upvote"
          disabled={rateMutation.isPending}
          onClick={() => handleRate('up')}
          className="h-6 w-6 flex items-center justify-center text-faint hover:text-[var(--ink-blue,#0b3a8a)] transition-colors disabled:pointer-events-none"
        >
          <ThumbsUp className="h-3.5 w-3.5" />
        </button>
        <button
          aria-label="Downvote"
          disabled={rateMutation.isPending}
          onClick={() => handleRate('down')}
          className="h-6 w-6 flex items-center justify-center text-faint hover:text-[var(--ink-blue,#0b3a8a)] transition-colors disabled:pointer-events-none"
        >
          <ThumbsDown className="h-3.5 w-3.5" />
        </button>
        <button
          aria-label="Save for later"
          disabled={rateMutation.isPending}
          onClick={() => handleRate('save')}
          className="h-6 w-6 flex items-center justify-center text-faint hover:text-[var(--ink-blue,#0b3a8a)] transition-colors disabled:pointer-events-none"
        >
          <Bookmark className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

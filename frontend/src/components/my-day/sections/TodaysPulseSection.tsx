import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { Skeleton } from '@/components/ui/skeleton';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { PulseRow } from './PulseRow';
import { ApiError, fetchPulseToday } from '@/lib/api';
import { useJobStore } from '@/stores/job-store';
import type { PulseDeck } from '@/types';

/** Format an ISO datetime string as a short HH:MM time. */
function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return iso;
  }
}

export function TodaysPulseSection() {
  const [expanded, setExpanded] = useState(false);

  const { data: deck, isLoading, isError } = useQuery<PulseDeck | null>({
    queryKey: QUERY_KEYS.pulse.today(),
    queryFn: fetchPulseToday,
  });

  const startJob = useJobStore((s) => s.startJob);
  const isGenerating = useJobStore((s) => s.hasRunning('pulse.generate'));

  const handleRegenerate = async () => {
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

  // Loading state: 3 skeleton rows
  if (isLoading) {
    return (
      <section id="pulse">
        <SectionHeader marker="Today's pulse" />
        <div className="space-y-0">
          {[0, 1, 2].map((i) => (
            <div key={i} className="grid grid-cols-[28px_1fr_auto] gap-4 py-3 border-b border-hair">
              <Skeleton className="h-3 w-5 mt-1" />
              <div className="space-y-2">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3 w-1/2" />
                <Skeleton className="h-3 w-full" />
              </div>
              <div className="flex flex-col gap-1">
                <Skeleton className="h-6 w-6" />
                <Skeleton className="h-6 w-6" />
                <Skeleton className="h-6 w-6" />
              </div>
            </div>
          ))}
        </div>
      </section>
    );
  }

  // Error state
  if (isError) {
    return (
      <section id="pulse">
        <SectionHeader
          marker="Today's pulse"
          meta="unavailable"
        />
        <p className="text-[12.5px] text-faint italic">
          Could not load today's pulse.
        </p>
      </section>
    );
  }

  const cards = deck?.cards ?? [];

  // Empty: only rank 1 (or none) — rank 2+ lives here; nothing to show
  if (cards.length <= 1) return null;

  const generatedAt = deck?.generated_at;
  const tailCards = expanded ? cards.slice(1) : cards.slice(1, 5);
  const hiddenCount = cards.length - 5;

  return (
    <section id="pulse">
      <SectionHeader
        marker="Today's pulse"
        meta={generatedAt ? `generated ${formatTime(generatedAt)}` : undefined}
        right={
          <div className="flex items-center gap-2">
            <Link
              to="/pulse"
              className="text-[11px] font-mono text-meta hover:text-[var(--ink-blue,#0b3a8a)] transition-colors"
            >
              archive →
            </Link>
            <button
              onClick={() => void handleRegenerate()}
              disabled={isGenerating}
              className="inline-flex items-center gap-1 text-[11px] font-mono text-meta hover:text-[var(--ink-blue,#0b3a8a)] disabled:opacity-40 disabled:pointer-events-none transition-colors"
            >
              <Sparkles className="h-3 w-3" />
              {isGenerating ? 'generating…' : 'regenerate'}
            </button>
          </div>
        }
      />

      <div>
        {tailCards.map((card, idx) => (
          <PulseRow key={card.card_id} card={card} rank={idx + 2} />
        ))}
      </div>

      {/* Show-more / collapse toggle */}
      {cards.length > 5 && (
        <button
          onClick={() => setExpanded((prev) => !prev)}
          className="mt-2 text-[11px] font-mono text-faint hover:text-[var(--ink-blue,#0b3a8a)] transition-colors"
        >
          {expanded
            ? 'show less ▴'
            : `show ${hiddenCount} more ▾`}
        </button>
      )}
    </section>
  );
}

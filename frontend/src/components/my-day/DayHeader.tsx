import { useRef } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Skeleton } from '@/components/ui/skeleton';
import { fetchMyDay, fetchPulseToday, fetchFeedPapers } from '@/lib/api';
import type { MyDayResponse, PulseDeck } from '@/types';

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 17) return 'Good afternoon';
  return 'Good evening';
}

function formatDate(): string {
  return new Intl.DateTimeFormat('en-US', {
    weekday: 'long',
    month: 'short',
    day: 'numeric',
  }).format(new Date());
}

interface DayHeaderProps {
  pulseCardRef?: React.RefObject<HTMLDivElement | null>;
  focusRef?: React.RefObject<HTMLDivElement | null>;
}

export function DayHeader({ pulseCardRef, focusRef }: DayHeaderProps) {
  const { data: myDay, isLoading: myDayLoading } = useQuery<MyDayResponse>({
    queryKey: ['my-day'],
    queryFn: fetchMyDay,
    refetchInterval: 60_000,
  });

  const { data: pulseDeck, isLoading: pulseLoading } = useQuery<PulseDeck | null>({
    queryKey: ['pulse-today'],
    queryFn: fetchPulseToday,
  });

  const { data: unprocessedFeed, isLoading: uploadsLoading } = useQuery({
    queryKey: ['feed-unprocessed-count'],
    queryFn: () => fetchFeedPapers({ statuses: 'new', limit: 1 }),
    refetchInterval: 120_000,
  });

  const isLoading = myDayLoading || pulseLoading || uploadsLoading;

  const pulseCount = pulseDeck?.card_count ?? 0;
  const cardsDue = myDay?.cards_due ?? 0;
  const tasksToday = myDay?.tasks.filter((t) => t.status !== 'done').length ?? 0;
  const unprocessedCount = unprocessedFeed?.total ?? 0;

  const handleScrollTo = (ref?: React.RefObject<HTMLDivElement | null>) => {
    ref?.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="space-y-1">
      <h1 className="text-3xl font-bold">{greeting()}</h1>
      <p className="text-muted-foreground">{formatDate()}</p>

      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {/* Pulse count */}
        <button
          type="button"
          onClick={() => handleScrollTo(pulseCardRef)}
          className="rounded-lg border bg-card p-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {isLoading ? (
            <Skeleton className="h-8 w-16" />
          ) : (
            <>
              <p className="text-2xl font-bold tabular-nums">{pulseCount}</p>
              <p className="text-xs text-muted-foreground">Pulse papers</p>
            </>
          )}
        </button>

        {/* Cards due */}
        <Link
          to="/cards?filter=due"
          className="rounded-lg border bg-card p-3 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {isLoading ? (
            <Skeleton className="h-8 w-16" />
          ) : (
            <>
              <p className="text-2xl font-bold tabular-nums">{cardsDue}</p>
              <p className="text-xs text-muted-foreground">Cards due</p>
            </>
          )}
        </Link>

        {/* Tasks today */}
        <button
          type="button"
          onClick={() => handleScrollTo(focusRef)}
          className="rounded-lg border bg-card p-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {isLoading ? (
            <Skeleton className="h-8 w-16" />
          ) : (
            <>
              <p className="text-2xl font-bold tabular-nums">{tasksToday}</p>
              <p className="text-xs text-muted-foreground">Tasks today</p>
            </>
          )}
        </button>

        {/* Unprocessed uploads */}
        <Link
          to="/feed"
          className="rounded-lg border bg-card p-3 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {isLoading ? (
            <Skeleton className="h-8 w-16" />
          ) : (
            <>
              <p className="text-2xl font-bold tabular-nums">{unprocessedCount}</p>
              <p className="text-xs text-muted-foreground">Unprocessed uploads</p>
            </>
          )}
        </Link>
      </div>
    </div>
  );
}

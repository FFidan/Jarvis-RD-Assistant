import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchMyDay, fetchPulseToday, fetchFeed } from '@/lib/api';
import type { MyDayResponse, PulseDeck } from '@/types';

interface MiniStatProps {
  value: number | string;
  label: string;
  onClick: () => void;
}

function MiniStat({ value, label, onClick }: MiniStatProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-col items-end gap-0.5 hover:opacity-70 transition-opacity focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 rounded"
    >
      <span className="font-mono text-[14px] font-semibold leading-none text-strong tabular-nums">
        {value}
      </span>
      <span className="font-mono text-[9px] uppercase tracking-wider text-meta">
        {label}
      </span>
    </button>
  );
}

export function DateMasthead() {
  const navigate = useNavigate();

  // Re-render once a minute so the time/date stays current across midnight
  // and across long-lived sessions. The mounted-now timestamp is the source
  // of truth for the displayed date and time.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  const dateStr =
    now.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' }) + '.';
  const timeStr = now.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });

  const { data: myDay } = useQuery<MyDayResponse>({
    queryKey: QUERY_KEYS.myDay.today(),
    queryFn: fetchMyDay,
    refetchInterval: 60_000,
  });

  const { data: pulseDeck } = useQuery<PulseDeck | null>({
    queryKey: QUERY_KEYS.pulse.today(),
    queryFn: fetchPulseToday,
    staleTime: 60_000,
  });

  const { data: unprocessedFeed } = useQuery({
    queryKey: QUERY_KEYS.actionItems.unprocessed(),
    queryFn: () => fetchFeed({ view: 'inbox', limit: 10 }),
    refetchInterval: 60_000,
  });

  const pulseCount = pulseDeck?.card_count ?? 0;
  const cardsDue = myDay?.cards_due ?? 0;
  const tasksToday = myDay?.tasks.filter((t) => t.status !== 'done').length ?? 0;
  const newCount = unprocessedFeed?.total ?? 0;

  const scrollTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <header className="border-b border-hair pb-6 flex flex-col gap-4 sm:grid sm:grid-cols-[1fr_auto] sm:gap-8 sm:items-end">
      <div className="min-w-0">
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-meta mb-2">
          RESEARCH LOG · {timeStr}
        </p>
        <h1 className="font-serif text-[36px] leading-tight text-strong">{dateStr}</h1>
      </div>

      <div className="flex flex-wrap sm:flex-nowrap gap-x-5 gap-y-1 text-right justify-end max-w-full">
        <MiniStat
          value={pulseCount}
          label="pulse"
          onClick={() => scrollTo('now')}
        />
        <MiniStat
          value={cardsDue}
          label="due"
          onClick={() => navigate('/cards')}
        />
        <MiniStat
          value={tasksToday}
          label="tasks"
          onClick={() => scrollTo('intent')}
        />
        <MiniStat
          value={newCount}
          label="new"
          onClick={() => navigate('/feed')}
        />
      </div>
    </header>
  );
}

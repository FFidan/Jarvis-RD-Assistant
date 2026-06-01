import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchMyDay, fetchPulseToday, fetchFeedPapers } from '@/lib/api';
import type { MyDayResponse, PulseDeck } from '@/types';

const ATTRIBUTED_QUOTES = [
  { text: '"Read deeply. Think slowly. Note generously."', author: '—Anon.' },
  { text: '"The questions you ask shape the answers you find."', author: '—Anon.' },
  { text: '"What is one paper you wish you understood better?"', author: '—Anon.' },
  { text: '"Today is for finishing what tomorrow remembers."', author: '—Anon.' },
];

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
  // of truth for date/time/quote/entry-num.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  const hash = Array.from(now.toDateString()).reduce((a, c) => a + c.charCodeAt(0), 0);
  // use hash (already computed) to pick a quote; fallback to first entry (array is a non-empty constant)
  const attributedQuote = ATTRIBUTED_QUOTES[hash % ATTRIBUTED_QUOTES.length] ?? ATTRIBUTED_QUOTES[0] ?? { text: '', author: '' };
  // TODO: replace with real journal entry count from journal_entries table
  const entryNum = Math.floor((now.getTime() - new Date('2026-01-01').getTime()) / 86400000);
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
    queryFn: () => fetchFeedPapers({ statuses: 'new', limit: 10 }),
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
    <header className="border-b border-hair pb-6 grid grid-cols-[1fr_auto] gap-8 items-end">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-meta mb-2">
          RESEARCH LOG · ENTRY {entryNum} · {timeStr}
        </p>
        <h1 className="font-serif text-[36px] leading-tight text-strong">{dateStr}</h1>
        <p className="font-serif italic text-[15px] text-zinc-600 dark:text-zinc-400 mt-1">
          {attributedQuote.text}{' '}
          <span className="not-italic font-mono text-[11px] text-meta">{attributedQuote.author}</span>
        </p>
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

/**
 * KpiBand — three-column KPI summary for the Reflect / Analytics page.
 *
 * Accepts an `AnalyticsSummaryResponse` and renders:
 *   PAPERS READ       | FOCUS HOURS     | CARDS REVIEWED
 *   <current total>   | <current total> | <current total>
 *   +N vs prev        | ±N vs prev      | N-day streak
 */
import type { AnalyticsSummaryResponse } from '@/types';
import { cn } from '@/lib/utils';

interface KpiBandProps {
  data: AnalyticsSummaryResponse;
}

interface TrendChipProps {
  delta: number;
  unit?: string;
}

function TrendChip({ delta, unit = '' }: TrendChipProps) {
  if (delta === 0) {
    return (
      <span className="font-mono text-[11px] tracking-wide text-faint">
        = vs prev
      </span>
    );
  }
  const sign = delta > 0 ? '+' : '';
  const formatted =
    Math.abs(delta) < 10 && !Number.isInteger(delta)
      ? `${sign}${delta.toFixed(1)}`
      : `${sign}${delta}`;
  return (
    <span
      className={cn(
        'font-mono text-[11px] tracking-wide',
        delta > 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400',
      )}
      data-testid="trend-chip"
    >
      {formatted}{unit} vs prev
    </span>
  );
}

interface StreakChipProps {
  days: number;
  label: string;
}

function StreakChip({ days, label }: StreakChipProps) {
  return (
    <span
      className="font-mono text-[11px] tracking-wide text-ink-blue"
      data-testid="streak-chip"
    >
      {days}-day {label}
    </span>
  );
}

interface KpiCellProps {
  label: string;
  value: string | number;
  trend: React.ReactNode;
}

function KpiCell({ label, value, trend }: KpiCellProps) {
  return (
    <div className="flex flex-col gap-1 py-4">
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-meta">
        {label}
      </span>
      <span
        className="font-serif text-[2.25rem] leading-none tracking-tight text-strong"
        data-testid="kpi-value"
      >
        {value}
      </span>
      <div className="mt-0.5">{trend}</div>
    </div>
  );
}

export function KpiBand({ data }: KpiBandProps) {
  const papersDelta = data.papers_read_total - data.papers_read_prev;
  const focusDelta = parseFloat(
    (data.focus_hours_total - data.focus_hours_prev).toFixed(1),
  );
  const cardsDelta = data.cards_reviewed_total - data.cards_reviewed_prev;

  const focusHoursDisplay =
    Number.isInteger(data.focus_hours_total)
      ? data.focus_hours_total
      : data.focus_hours_total.toFixed(1);

  return (
    <div
      className="grid grid-cols-3 divide-x divide-hair border-y border-hair"
      data-testid="kpi-band"
    >
      {/* PAPERS READ */}
      <div className="px-4 first:pl-0">
        <KpiCell
          label="PAPERS READ"
          value={data.papers_read_total}
          trend={<TrendChip delta={papersDelta} />}
        />
      </div>

      {/* FOCUS HOURS */}
      <div className="px-4">
        <KpiCell
          label="FOCUS HOURS"
          value={focusHoursDisplay}
          trend={<TrendChip delta={focusDelta} unit="h" />}
        />
      </div>

      {/* CARDS REVIEWED */}
      <div className="px-4 last:pr-0">
        <KpiCell
          label="CARDS REVIEWED"
          value={data.cards_reviewed_total}
          trend={
            data.cards_review_streak_days > 0 ? (
              <StreakChip days={data.cards_review_streak_days} label="streak" />
            ) : (
              <TrendChip delta={cardsDelta} />
            )
          }
        />
      </div>
    </div>
  );
}

import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Clock } from 'lucide-react';
import { getStats, fetchMyDay } from '@/lib/api';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { Progress } from '@/components/ui/progress';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import type { RetentionStats, MyDayResponse } from '@/types';

export function LearningFocusSection() {
  const navigate = useNavigate();

  // Reuses same query key as LearningCardsSummary — deduped in React Query cache
  const { data: stats } = useQuery<RetentionStats>({
    queryKey: QUERY_KEYS.retention.stats(),
    queryFn: getStats,
    refetchInterval: 120_000,
  });

  // Reuses the shared ['my-day'] query key (deduped with IntentSection et al.)
  const { data: myDay } = useQuery<MyDayResponse>({
    queryKey: QUERY_KEYS.myDay.today(),
    queryFn: fetchMyDay,
  });

  const phase = usePomodoroStore((s) => s.phase);
  const attachedItem = usePomodoroStore((s) => s.attachedItem);

  const todayFocusHours = myDay?.today_focus_hours ?? 0;
  const focusStreakDays = myDay?.focus_streak_days ?? 0;

  const focusProgress = Math.min(100, (todayFocusHours / 4) * 100);

  return (
    <section id="learning-focus">
      <SectionHeader marker="Learning &amp; focus" />

      <div className="grid grid-cols-2 gap-4">
        {/* ── Learning cards card (left) ── */}
        <div className="rounded-lg border border-hair bg-card p-4">
          <h3 className="font-mono text-[10px] uppercase tracking-wide text-meta mb-2">
            Learning cards
          </h3>

          {stats ? (
            <>
              {stats.due_now > 0 ? (
                <>
                  <div className="bg-[hsl(var(--cta-warn-bg))] border border-[hsl(var(--cta-warn-border))] rounded-md p-3">
                    <p className="text-[24px] font-bold tabular-nums text-[hsl(var(--cta-warn-fg))]">
                      {stats.due_now}
                    </p>
                    <p className="text-[11px] font-mono uppercase tracking-wide text-[hsl(var(--cta-warn-fg))] opacity-80">
                      cards due now
                    </p>
                  </div>
                  <button
                    aria-label={`Review ${stats.due_now} cards now`}
                    onClick={() => navigate('/cards')}
                    className="mt-3 w-full bg-[hsl(var(--cta-warn-solid))] hover:bg-[hsl(var(--cta-warn-solid))] text-white rounded-md py-2 text-sm font-medium transition-colors"
                  >
                    Review now →
                  </button>
                </>
              ) : (
                <p className="text-faint italic text-sm">No reviews pending. ✓</p>
              )}

              <p className="font-mono text-[10px] text-meta mt-3">
                {stats.streak_days}d streak
                {' · '}
                {stats.reviewed_today} reviewed today
                {' · '}
                {Math.round((stats.average_retention || 0) * 100)}% 30d retention
              </p>
            </>
          ) : (
            <p className="text-faint text-sm italic">Loading…</p>
          )}
        </div>

        {/* ── Focus today card (right) ── */}
        <div className="rounded-lg border border-hair bg-card p-4">
          <div className="flex items-center justify-between mb-2">
            <h3 className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-wide text-meta">
              <Clock className="h-3 w-3" />
              Focus today
            </h3>
            <button
              aria-label={`Start 25-minute focus session${phase !== 'idle' ? ' (timer running)' : ''}`}
              onClick={() => usePomodoroStore.getState().startWork()}
              disabled={phase !== 'idle'}
              className="text-[11px] font-mono text-white bg-[var(--ink-blue)] hover:opacity-90 px-2 py-1 rounded disabled:opacity-40 transition-opacity"
            >
              Start 25:00
            </button>
          </div>

          <p className="text-[24px] font-bold tabular-nums text-strong">
            {todayFocusHours.toFixed(1)}h
            <span className="text-faint text-[14px]"> / 4h target</span>
          </p>

          <Progress value={focusProgress} className="h-1 mt-2" />

          <p className="font-mono text-[10px] text-meta mt-2">
            {focusStreakDays}d streak
            {attachedItem?.title ? ` · last: ${attachedItem.title}` : ''}
          </p>
        </div>
      </div>
    </section>
  );
}

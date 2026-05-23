import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Check, ChevronRight } from 'lucide-react';
import { toast } from 'sonner';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { fetchYesterday, updateTask } from '@/lib/api';
import type { YesterdaySummary } from '@/types';

/**
 * § Yesterday — on-the-fly rollup of yesterday's activity (prototype :72-91).
 * Hidden entirely when there was no recorded activity so the page does not
 * lead with an empty card.
 */
export function YesterdaySection() {
  const queryClient = useQueryClient();

  // Browser tz: minutes EAST of UTC (JS getTimezoneOffset is minutes WEST).
  const tzOffsetMinutes = -new Date().getTimezoneOffset();

  const { data, isError } = useQuery<YesterdaySummary>({
    queryKey: QUERY_KEYS.myDay.yesterday(tzOffsetMinutes),
    queryFn: () => fetchYesterday(tzOffsetMinutes),
    staleTime: 5 * 60_000,
  });

  const carryOverMutation = useMutation({
    mutationFn: (taskId: number) => updateTask(taskId, { status: 'todo' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() });
      toast.success('Carried over to today');
    },
    onError: (err: Error) => toast.error(`Couldn't carry over: ${err.message}`),
  });

  // Silent when the query failed or there is genuinely nothing to show —
  // § Yesterday is a launchpad, not a placeholder.
  if (isError || !data) return null;

  const { focused_hours, cards_reviewed, tasks_done, completed, deferred } = data;
  if (completed.length === 0 && deferred.length === 0) return null;

  return (
    <section id="yesterday">
      <SectionHeader
        marker="Yesterday"
        meta={`${focused_hours.toFixed(1)}h focused · ${cards_reviewed} cards · ${tasks_done} tasks done`}
      />

      <div className="space-y-1 pl-1 text-[13.5px] leading-relaxed text-soft">
        {completed.map((t) => (
          <div key={`c-${t.id}`} className="flex items-start gap-2.5">
            <Check className="h-3.5 w-3.5 text-emerald-600 mt-1 shrink-0" aria-hidden />
            <span>{t.title}</span>
          </div>
        ))}

        {deferred.map((t) => (
          <div key={`d-${t.id}`} className="flex items-start gap-2.5">
            <ChevronRight className="h-3.5 w-3.5 text-faint mt-1 shrink-0" aria-hidden />
            <span className="text-meta">
              <span className="line-through decoration-zinc-300">{t.title}</span>{' '}
              <button
                type="button"
                onClick={() => carryOverMutation.mutate(t.id)}
                disabled={carryOverMutation.isPending}
                className="ml-1 font-medium text-[var(--ink-blue,#0b3a8a)] hover:underline disabled:opacity-50"
              >
                carry over →
              </button>
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

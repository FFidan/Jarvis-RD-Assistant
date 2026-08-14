import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { GradientProgressBar } from '@/components/my-day/primitives/GradientProgressBar';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchThreads, resumeThread } from '@/lib/api';
import type { Thread } from '@/types';

function formatLastAt(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return iso;
  }
}

/**
 * Hero "Resume thread" mode (prototype :475-499). Surfaces the
 * most-recently-touched open thread (smart-hero "thread" mode).
 */
export function HeroThread() {
  const queryClient = useQueryClient();
  // The button starts the CONFIGURED duration — its label must say so.
  const workMinutes = usePomodoroStore((s) => s.workMinutes);

  const { data, isLoading, isError } = useQuery<Thread[]>({
    queryKey: QUERY_KEYS.myDay.threads(),
    queryFn: fetchThreads,
    staleTime: 60_000,
  });

  const resumeMutation = useMutation({
    mutationFn: (id: number) => resumeThread(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.threads() });
      toast.success('Thread resumed');
    },
    onError: (err: Error) => toast.error(`Couldn't resume: ${err.message}`),
  });

  if (isError) {
    return (
      <p className="text-faint italic font-serif text-center py-8">
        Couldn't load threads — check your connection.
      </p>
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="h-4 w-full" />
      </div>
    );
  }

  // Most-recently-touched open thread.
  const thread = (data ?? [])
    .filter((t) => t.status === 'open')
    .sort((a, b) => new Date(b.last_at).getTime() - new Date(a.last_at).getTime())[0];

  if (!thread) {
    return (
      <p className="text-faint italic font-serif text-center py-8">
        No open threads — start one in Open threads below.
      </p>
    );
  }

  const pct = Math.round((thread.progress ?? 0) * 100);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center rounded-full bg-[var(--ink-blue,#0b3a8a)] px-2.5 py-0.5 text-[10px] font-mono font-semibold text-white">
          Resume
        </span>
        <span className="font-mono text-[10.5px] uppercase tracking-[0.15em] text-faint">
          {pct}% · last touched {formatLastAt(thread.last_at)}
        </span>
      </div>

      <h2 className="font-serif text-[24px] leading-[1.2] tracking-tight max-w-[36ch] text-strong">
        {thread.title}
      </h2>

      {thread.anchor && (
        <p className="font-serif italic text-[14px] text-meta">↳ {thread.anchor}</p>
      )}

      <div className="flex items-center gap-3 max-w-md">
        <div className="max-w-[200px] flex-1">
          <GradientProgressBar value={pct} color="var(--ink-blue, #0b3a8a)" />
        </div>
        <span className="font-mono text-[11px] text-soft tabular-nums">{pct}%</span>
      </div>

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          size="sm"
          className="bg-[var(--ink-blue,#0b3a8a)] text-white hover:bg-[var(--ink-blue,#0b3a8a)]/90"
          onClick={() => resumeMutation.mutate(thread.id)}
          disabled={resumeMutation.isPending}
        >
          Resume thread
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            usePomodoroStore
              .getState()
              .startWork({ id: thread.id, title: thread.title, type: 'task' });
            toast.success(`${workMinutes}-min focus started`);
          }}
        >
          Start {workMinutes}-min focus
        </Button>
      </div>
    </div>
  );
}

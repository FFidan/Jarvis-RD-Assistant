import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { updateTask } from '@/lib/api';

/** Format seconds as mm:ss */
function formatMmSs(totalSeconds: number): string {
  const secs = Math.max(0, Math.round(totalSeconds));
  const mm = String(Math.floor(secs / 60)).padStart(2, '0');
  const ss = String(secs % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

export function HeroTask() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const phase = usePomodoroStore((s) => s.phase);
  const attachedItem = usePomodoroStore((s) => s.attachedItem);
  const pausedAt = usePomodoroStore((s) => s.pausedAt);
  const secondsRemaining = usePomodoroStore((s) => s.secondsRemaining);

  const doneMutation = useMutation({
    mutationFn: (taskId: number) => updateTask(taskId, { status: 'done' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['my-day'] });
      toast.success('Task marked done');
    },
    onError: (err: Error) => {
      toast.error(`Failed to mark done: ${err.message ?? 'unknown error'}`);
    },
  });

  const isActive = phase !== 'idle' && attachedItem !== null;

  if (!isActive) {
    return (
      <p className="text-faint italic font-serif text-center py-8">
        No active task. Start one from the Tasks ladder below.
      </p>
    );
  }

  // isActive is true here — attachedItem is non-null
  const item = attachedItem!;
  const isPaused = pausedAt !== null;

  // Timer display: ~N min remaining (work phase only; secondsRemaining frozen at pause value when paused)
  const minRemaining = secondsRemaining > 0 ? Math.ceil(secondsRemaining / 60) : null;

  // Resume button label: show frozen clock position when paused
  const resumeLabel = secondsRemaining > 0
    ? `Resume (${formatMmSs(secondsRemaining)})`
    : 'Resume Pomodoro';

  // Project color badge: AttachedItem has no project_color field — use ink-blue as default
  const badgeColor = 'var(--ink-blue, #0b3a8a)';

  return (
    <div className="space-y-4">
      {/* Header: pill + phase label + timer */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-[10px] font-mono font-semibold"
          style={{ borderColor: badgeColor, color: badgeColor }}>
          {item.type === 'paper' ? 'paper' : 'task'}
        </span>
        <span className="font-mono text-[11px] text-faint">
          {phase === 'work' ? (isPaused ? 'paused' : 'Pomodoro running') : `Phase: ${phase}`}
        </span>
        {minRemaining !== null && phase === 'work' && (
          <span className="font-mono text-[11px] text-faint">
            · ~{minRemaining} min remaining
          </span>
        )}
      </div>

      {/* Task / paper title */}
      <h2 className="font-serif text-[24px] leading-[1.18] tracking-tight max-w-[36ch] text-strong">
        {item.title}
      </h2>

      {/* CTA buttons */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        {isPaused ? (
          <Button
            size="sm"
            className="bg-[var(--ink-blue,#0b3a8a)] text-white hover:bg-[var(--ink-blue,#0b3a8a)]/90"
            onClick={() => usePomodoroStore.getState().resume()}
          >
            {resumeLabel}
          </Button>
        ) : (
          <span className="font-mono text-[11px] text-faint italic">Pomodoro running…</span>
        )}

        {item.type === 'paper' && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => navigate(`/paper/${item.id}`)}
          >
            Open paper
          </Button>
        )}

        {item.type === 'task' && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => doneMutation.mutate(item.id)}
            disabled={doneMutation.isPending}
          >
            Mark done
          </Button>
        )}
      </div>
    </div>
  );
}

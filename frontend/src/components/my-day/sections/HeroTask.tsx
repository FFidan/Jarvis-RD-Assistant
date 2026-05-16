import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { updateTask, logFocusSession } from '@/lib/api';

/** Format seconds as mm:ss */
function formatMmSs(totalSeconds: number): string {
  const secs = Math.max(0, Math.round(totalSeconds));
  const mm = String(Math.floor(secs / 60)).padStart(2, '0');
  const ss = String(secs % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

/**
 * Cycle progress dots — one per target cycle. Filled dots = completed work
 * cycles; the current in-progress cycle is shown as a ring.
 */
function CycleDots({
  completed,
  target,
  inProgress,
}: {
  completed: number;
  target: number;
  inProgress: boolean;
}) {
  const dots = Array.from({ length: Math.max(1, target) }, (_, i) => i);
  return (
    <div
      className="flex items-center gap-1"
      role="img"
      aria-label={`Pomodoro cycle ${Math.min(completed + (inProgress ? 1 : 0), target)} of ${target}`}
    >
      {dots.map((i) => {
        const isDone = i < completed;
        const isCurrent = i === completed && inProgress;
        return (
          <span
            key={i}
            data-testid="cycle-dot"
            data-state={isDone ? 'done' : isCurrent ? 'current' : 'pending'}
            className={
              isDone
                ? 'h-1.5 w-1.5 rounded-full bg-[var(--ink-blue,#0b3a8a)]'
                : isCurrent
                  ? 'h-1.5 w-1.5 rounded-full ring-1 ring-[var(--ink-blue,#0b3a8a)]'
                  : 'h-1.5 w-1.5 rounded-full bg-zinc-200 dark:bg-zinc-700'
            }
          />
        );
      })}
    </div>
  );
}

export function HeroTask() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const phase = usePomodoroStore((s) => s.phase);
  const attachedItem = usePomodoroStore((s) => s.attachedItem);
  const pausedAt = usePomodoroStore((s) => s.pausedAt);
  const secondsRemaining = usePomodoroStore((s) => s.secondsRemaining);
  const cyclesCompleted = usePomodoroStore((s) => s.cyclesCompleted);
  const targetCycles = usePomodoroStore((s) => s.targetCycles);

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

  const logMutation = useMutation({
    mutationFn: logFocusSession,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['my-day'] }),
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
  const isWork = phase === 'work';
  const isBreak = phase === 'short-break' || phase === 'long-break';

  // Timer display: ~N min remaining (work phase only; secondsRemaining frozen at pause value when paused)
  const minRemaining = secondsRemaining > 0 ? Math.ceil(secondsRemaining / 60) : null;

  // Resume button label: show frozen clock position when paused
  const resumeLabel =
    secondsRemaining > 0 ? `Resume (${formatMmSs(secondsRemaining)})` : 'Resume Pomodoro';

  // Project color badge: AttachedItem has no project_color field — use ink-blue as default
  const badgeColor = 'var(--ink-blue, #0b3a8a)';

  /** Stop the current session, log the elapsed focus time, and reset. */
  const handleStopAndLog = () => {
    const result = usePomodoroStore.getState().stopAndLog();
    if (!result) return;
    if (result.durationSeconds >= 1) {
      logMutation.mutate({
        duration_hours: result.durationSeconds / 3600,
        task_id: result.taskId,
        paper_id: result.paperId,
      });
      toast.success(`Logged ${Math.round(result.durationSeconds / 60)} min of focus`);
    } else {
      toast.message('Pomodoro stopped (too short to log)');
    }
  };

  return (
    <div className="space-y-4">
      {/* Header: pill + phase label + timer + cycle dots */}
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-[10px] font-mono font-semibold"
          style={{ borderColor: badgeColor, color: badgeColor }}
        >
          {item.type === 'paper' ? 'paper' : 'task'}
        </span>
        <span className="font-mono text-[11px] text-faint">
          {isWork
            ? isPaused
              ? 'paused'
              : 'Pomodoro running'
            : phase === 'short-break'
              ? 'short break'
              : phase === 'long-break'
                ? 'long break'
                : `Phase: ${phase}`}
        </span>
        {minRemaining !== null && isWork && (
          <span className="font-mono text-[11px] text-faint">· ~{minRemaining} min remaining</span>
        )}
        <span className="ml-auto">
          <CycleDots
            completed={cyclesCompleted}
            target={targetCycles}
            inProgress={isWork}
          />
        </span>
      </div>

      {/* Task / paper title */}
      <h2 className="font-serif text-[24px] leading-[1.18] tracking-tight max-w-[36ch] text-strong">
        {item.title}
      </h2>

      {/* CTA buttons */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        {/* Pause / Resume — only meaningful during a work phase */}
        {isWork &&
          (isPaused ? (
            <Button
              size="sm"
              className="bg-[var(--ink-blue,#0b3a8a)] text-white hover:bg-[var(--ink-blue,#0b3a8a)]/90"
              onClick={() => usePomodoroStore.getState().resume()}
            >
              {resumeLabel}
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={() => usePomodoroStore.getState().pause()}
            >
              Pause
            </Button>
          ))}

        {/* Skip break — jump straight back to a fresh work cycle */}
        {isBreak && (
          <Button
            size="sm"
            className="bg-[var(--ink-blue,#0b3a8a)] text-white hover:bg-[var(--ink-blue,#0b3a8a)]/90"
            onClick={() => usePomodoroStore.getState().skipBreak()}
          >
            Skip break
          </Button>
        )}

        {/* Stop & log — end the session and record elapsed focus time */}
        <Button
          size="sm"
          variant="outline"
          onClick={handleStopAndLog}
          disabled={logMutation.isPending}
        >
          Stop &amp; log
        </Button>

        {item.type === 'paper' && (
          <Button size="sm" variant="outline" onClick={() => navigate(`/paper/${item.id}`)}>
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

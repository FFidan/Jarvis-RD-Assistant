import { useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { updateTask, deleteTask } from '@/lib/api';
import type { MyDayTask } from '@/types';

interface TaskRowProps {
  task: MyDayTask;
  index: number;
  isTimerActive: boolean;
}

export function TaskRow({ task, index, isTimerActive }: TaskRowProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // The button starts the CONFIGURED duration — its tooltip must say so.
  const workMinutes = usePomodoroStore((s) => s.workMinutes);

  const completeMutation = useMutation({
    mutationFn: () => updateTask(task.id, { status: 'done' }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() }),
    onError: (err: Error) => {
      toast.error(`Failed to mark done: ${err.message ?? 'unknown error'}`);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteTask(task.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() }),
    onError: (err: Error) => {
      toast.error(`Failed to delete task: ${err.message ?? 'unknown error'}`);
    },
  });

  return (
    <div className="group flex items-center gap-3 py-2 hover:bg-white/60 dark:hover:bg-zinc-900/60 -mx-2 px-2 rounded-md">
      {/* Row index */}
      <span className="font-mono text-[10px] text-faint tabular-nums w-5 shrink-0">
        {(index + 1).toString().padStart(2, '0')}
      </span>

      {/* Completion circle — 44px tap target on touch (data-touch-target); the
          small dot is an inner span so the visual stays 14px while the hit-area
          lifts to 44px on touch devices. */}
      <button
        data-touch-target
        onClick={() => completeMutation.mutate()}
        disabled={completeMutation.isPending}
        className="flex items-center justify-center shrink-0 disabled:opacity-40"
        aria-label="Mark task done"
      >
        <span
          className={`h-3.5 w-3.5 rounded-full border-[1.5px] hover:bg-[var(--ink-blue-soft)] transition-colors ${
            index === 0 ? 'border-[var(--ink-blue)]' : 'border-zinc-300 dark:border-zinc-700'
          }`}
        />
      </button>

      {/* Title */}
      <span className="text-[13.5px] text-soft group-hover:text-strong truncate flex-1">
        {task.title}
      </span>

      {/* Project badge */}
      {task.project_name && (
        <button
          onClick={() => navigate('/projects', { state: { projectId: task.project_id } })}
          className="text-[10px] font-mono px-1.5 py-0.5 rounded border shrink-0"
          style={
            task.project_color
              ? { borderColor: task.project_color, color: task.project_color }
              : undefined
          }
        >
          {task.project_name}
        </button>
      )}

      {/* Focus button — always visible; binds this task to the Pomodoro */}
      <button
        data-touch-target
        onClick={() =>
          usePomodoroStore.getState().startWork({ id: task.id, title: task.title, type: 'task' })
        }
        disabled={isTimerActive}
        className="h-6 px-2 text-[10px] font-mono rounded text-[var(--ink-blue)] hover:bg-[var(--ink-blue-soft)] disabled:opacity-30 transition-colors shrink-0"
        title={
          isTimerActive
            ? 'A Pomodoro is already running'
            : `Start ${workMinutes}:00 Pomodoro on this task`
        }
      >
        ▶ Focus
      </button>

      {/* Delete button */}
      <button
        data-touch-target
        onClick={() => deleteMutation.mutate()}
        disabled={deleteMutation.isPending}
        className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100 h-6 w-6 flex items-center justify-center text-faint hover:text-red-600 transition-opacity shrink-0"
        aria-label="Delete task"
      >
        ✕
      </button>
    </div>
  );
}

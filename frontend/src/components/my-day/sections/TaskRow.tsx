import { useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
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

  const completeMutation = useMutation({
    mutationFn: () => updateTask(task.id, { status: 'done' }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['my-day'] }),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteTask(task.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['my-day'] }),
  });

  return (
    <div className="group flex items-center gap-3 py-2 hover:bg-white/60 dark:hover:bg-zinc-900/60 -mx-2 px-2 rounded-md">
      {/* Row index */}
      <span className="font-mono text-[10px] text-faint tabular-nums w-5 flex-shrink-0">
        {(index + 1).toString().padStart(2, '0')}
      </span>

      {/* Completion circle */}
      <button
        onClick={() => completeMutation.mutate()}
        disabled={completeMutation.isPending}
        className={`h-3.5 w-3.5 rounded-full border-[1.5px] hover:bg-[var(--ink-blue-soft)] flex-shrink-0 transition-colors ${
          index === 0 ? 'border-[var(--ink-blue)]' : 'border-zinc-300 dark:border-zinc-700'
        }`}
        aria-label="Mark task done"
      />

      {/* Title */}
      <span className="text-[13.5px] text-soft group-hover:text-strong truncate flex-1">
        {task.title}
      </span>

      {/* Project badge */}
      {task.project_name && (
        <button
          onClick={() => navigate('/projects', { state: { projectId: task.project_id } })}
          className="text-[10px] font-mono px-1.5 py-0.5 rounded border flex-shrink-0"
          style={
            task.project_color
              ? { borderColor: task.project_color, color: task.project_color }
              : undefined
          }
        >
          {task.project_name}
        </button>
      )}

      {/* Focus button */}
      <button
        data-touch-target
        onClick={() =>
          usePomodoroStore.getState().startWork({ id: task.id, title: task.title, type: 'task' })
        }
        disabled={isTimerActive}
        className="opacity-0 group-hover:opacity-100 h-6 px-2 text-[10px] font-mono rounded text-[var(--ink-blue)] hover:bg-[var(--ink-blue-soft)] disabled:opacity-30 transition-opacity flex-shrink-0"
        title={isTimerActive ? 'A Pomodoro is already running' : 'Start 25:00 Pomodoro on this task'}
      >
        ▶ Focus
      </button>

      {/* Delete button */}
      <button
        data-touch-target
        onClick={() => deleteMutation.mutate()}
        disabled={deleteMutation.isPending}
        className="opacity-0 group-hover:opacity-100 h-6 w-6 flex items-center justify-center text-faint hover:text-red-600 transition-opacity flex-shrink-0"
        aria-label="Delete task"
      >
        ✕
      </button>
    </div>
  );
}

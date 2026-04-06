import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { updateTask } from '@/lib/api';
import type { MyDayTask } from '@/types';

interface TaskListProps {
  tasks: MyDayTask[];
}

export function TaskList({ tasks }: TaskListProps) {
  const [showCompleted, setShowCompleted] = useState(false);
  const [errorTaskId, setErrorTaskId] = useState<number | null>(null);
  const queryClient = useQueryClient();
  const pomodoroPhase = usePomodoroStore(s => s.phase);
  const pomodoroStartWork = usePomodoroStore(s => s.startWork);

  const completeMutation = useMutation({
    mutationFn: (taskId: number) => updateTask(taskId, { status: 'done' }),
    onSuccess: () => {
      setErrorTaskId(null);
      queryClient.invalidateQueries({ queryKey: ['my-day'] });
    },
    onError: (_err: Error, taskId: number) => setErrorTaskId(taskId),
  });

  const pendingTasks = tasks.filter((t) => t.status !== 'done');
  const completedTasks = tasks.filter((t) => t.status === 'done');

  const formatRelativeTime = (dateStr: string) => {
    const diff = Date.now() - new Date(dateStr).getTime();
    const hours = Math.floor(diff / 3600000);
    if (hours < 1) return 'just now';
    if (hours === 1) return '1h ago';
    return `${hours}h ago`;
  };

  const isTimerActive = pomodoroPhase !== 'idle';

  return (
    <div className="space-y-2">
      {/* Pending tasks */}
      {pendingTasks.length === 0 && (
        <p className="text-sm text-muted-foreground py-4 text-center">
          No tasks for today. Add one above!
        </p>
      )}
      {pendingTasks.map((task) => (
        <div
          key={task.id}
          className="flex items-center gap-3 py-2 px-1 rounded-md hover:bg-muted/50 group"
        >
          {/* Completion circle */}
          <button
            onClick={() => completeMutation.mutate(task.id)}
            disabled={completeMutation.isPending}
            className={`w-5 h-5 rounded-full border-2 flex-shrink-0 transition-colors hover:bg-primary/10 ${
              errorTaskId === task.id
                ? 'border-destructive hover:border-destructive'
                : 'border-muted-foreground/30 hover:border-primary'
            }`}
            aria-label={`Complete "${task.title}"`}
          />

          {/* Task title */}
          <span className="flex-1 text-sm truncate">{task.title}</span>

          {/* Project badge */}
          {task.project_name && (
            <Link to="/projects" className="flex-shrink-0">
              <Badge
                variant="outline"
                className="text-xs hover:bg-muted/50 transition-colors cursor-pointer"
                style={task.project_color ? { borderColor: task.project_color, color: task.project_color } : undefined}
              >
                {task.project_name}
              </Badge>
            </Link>
          )}

          {/* Focus button */}
          <Button
            variant="ghost"
            size="sm"
            className="opacity-0 group-hover:opacity-100 transition-opacity h-7 px-2 text-xs"
            disabled={isTimerActive}
            onClick={() =>
              pomodoroStartWork({ id: task.id, title: task.title, type: 'task' })
            }
          >
            ▶ Focus
          </Button>
        </div>
      ))}

      {/* Completed section */}
      {completedTasks.length > 0 && (
        <div className="pt-2 border-t">
          <button
            onClick={() => setShowCompleted(!showCompleted)}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            {showCompleted ? '▾' : '▸'} Completed ({completedTasks.length})
          </button>
          {showCompleted &&
            completedTasks.map((task) => (
              <div key={task.id} className="flex items-center gap-3 py-1.5 px-1">
                <div className="w-5 h-5 rounded-full bg-primary/20 flex items-center justify-center flex-shrink-0">
                  <span className="text-xs text-primary">✓</span>
                </div>
                <span className="flex-1 text-sm text-muted-foreground line-through truncate">
                  {task.title}
                </span>
                {task.completed_at && (
                  <span className="text-xs text-muted-foreground flex-shrink-0">
                    {formatRelativeTime(task.completed_at)}
                  </span>
                )}
              </div>
            ))}
        </div>
      )}
    </div>
  );
}

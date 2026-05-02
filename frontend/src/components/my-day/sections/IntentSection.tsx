import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchMyDay, createQuickTask } from '@/lib/api';
import { SectionHeader } from './SectionHeader';
import { TaskRow } from './TaskRow';

export function IntentSection() {
  const [showAddForm, setShowAddForm] = useState(false);
  const [addTitle, setAddTitle] = useState('');
  const [addPriority, setAddPriority] = useState<number>(3);
  const [showCompleted, setShowCompleted] = useState(false);

  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ['my-day'],
    queryFn: fetchMyDay,
    refetchInterval: 60_000,
  });

  const addMutation = useMutation({
    mutationFn: createQuickTask,
    onSuccess: () => {
      setAddTitle('');
      setAddPriority(3);
      setShowAddForm(false);
      queryClient.invalidateQueries({ queryKey: ['my-day'] });
    },
  });

  const handleAddSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = addTitle.trim();
    if (!trimmed || addMutation.isPending) return;
    addMutation.mutate({ title: trimmed, priority: addPriority });
  };

  const isTimerActive = usePomodoroStore((s) => s.phase) !== 'idle';

  const pendingTasks = data?.tasks.filter((t) => t.status !== 'done') ?? [];
  const completedToday = data?.tasks.filter((t) => t.status === 'done') ?? [];

  return (
    <section id="intent">
      <SectionHeader marker="Today's intent" />

      {/* Intent line */}
      <p className="font-serif text-[19px] leading-snug max-w-[58ch] border-l-2 border-[var(--ink-blue)] pl-5 mb-4">
        Today's intent will live here. Phase 1b/2 will let you set &amp; persist it.
      </p>

      {/* Tasks ladder */}
      <div className="pl-5 space-y-0">
        {pendingTasks.length === 0 && !showAddForm && (
          <p className="text-[12px] font-mono text-faint py-1">
            No tasks for today — add one below.
          </p>
        )}

        {pendingTasks.map((task, i) => (
          <TaskRow
            key={task.id}
            task={task}
            index={i}
            isTimerActive={isTimerActive}
          />
        ))}

        {/* Quick-add row */}
        {showAddForm ? (
          <form
            onSubmit={handleAddSubmit}
            className="flex items-center gap-2 py-1.5 -mx-2 px-2"
          >
            <span className="font-mono text-[10px] text-faint tabular-nums w-5 flex-shrink-0">
              {(pendingTasks.length + 1).toString().padStart(2, '0')}
            </span>
            <div className="h-3.5 w-3.5 rounded-full border-[1.5px] border-hair flex-shrink-0" />
            <input
              autoFocus
              value={addTitle}
              onChange={(e) => setAddTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  setShowAddForm(false);
                  setAddTitle('');
                }
              }}
              placeholder="Task title…"
              className="flex-1 text-[13.5px] text-soft bg-transparent outline-none placeholder:text-zinc-300 dark:placeholder:text-zinc-600"
              disabled={addMutation.isPending}
            />
            <select
              value={addPriority}
              onChange={(e) => setAddPriority(Number(e.target.value))}
              className="text-[11px] font-mono text-faint bg-transparent border border-hair rounded px-1 py-0.5 flex-shrink-0"
              disabled={addMutation.isPending}
            >
              <option value={1}>P1</option>
              <option value={2}>P2</option>
              <option value={3}>P3</option>
              <option value={4}>P4</option>
            </select>
            <button
              type="submit"
              disabled={addMutation.isPending || !addTitle.trim()}
              className="text-[11px] font-mono text-[var(--ink-blue)] hover:opacity-70 disabled:opacity-30 flex-shrink-0"
            >
              add
            </button>
            <button
              type="button"
              onClick={() => { setShowAddForm(false); setAddTitle(''); }}
              className="text-[11px] font-mono text-faint hover:text-soft flex-shrink-0"
            >
              cancel
            </button>
          </form>
        ) : (
          <button
            onClick={() => setShowAddForm(true)}
            className="flex items-center gap-2 text-[12px] font-mono text-faint hover:text-strong ml-8 mt-1 transition-colors"
          >
            + add task
          </button>
        )}

        {/* Completed-today footer */}
        {completedToday.length > 0 && (
          <div className="mt-2 pt-2 border-t border-zinc-100 dark:border-zinc-800">
            <button
              onClick={() => setShowCompleted((v) => !v)}
              className="text-[11px] font-mono text-faint hover:text-soft transition-colors"
            >
              {showCompleted ? '▾ Hide completed' : `▸ ${completedToday.length} done today`}
            </button>

            {showCompleted && (
              <div className="mt-1 space-y-0">
                {completedToday.map((task) => (
                  <div
                    key={task.id}
                    className="flex items-center gap-3 py-1.5 -mx-2 px-2"
                  >
                    <span className="font-mono text-[10px] text-zinc-300 dark:text-zinc-600 tabular-nums w-5 flex-shrink-0">
                      ✓
                    </span>
                    <span className="text-[13.5px] line-through text-faint truncate flex-1">
                      {task.title}
                    </span>
                    {task.project_name && (
                      <span
                        className="text-[10px] font-mono px-1.5 py-0.5 rounded border flex-shrink-0 text-zinc-300 dark:text-zinc-600 border-hair"
                      >
                        {task.project_name}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

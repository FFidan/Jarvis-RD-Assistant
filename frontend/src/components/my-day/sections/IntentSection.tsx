import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchMyDay, createQuickTask, updateTask, fetchIntentToday, saveIntentToday } from '@/lib/api';
import type { MyDayTask } from '@/types';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { TaskRow } from './TaskRow';

function CompletedRow({ task }: { task: MyDayTask }) {
  const queryClient = useQueryClient();
  const reopenMutation = useMutation({
    mutationFn: () => updateTask(task.id, { status: 'todo' }),
    onSuccess: () => toast.success('Task reopened'),
    onError: (err: Error) => toast.error(`Failed to reopen: ${err.message}`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() }),
  });

  return (
    <div className="group flex items-center gap-3 py-1.5 -mx-2 px-2 hover:bg-white/60 dark:hover:bg-zinc-900/60 rounded-md">
      <button
        onClick={() => reopenMutation.mutate()}
        disabled={reopenMutation.isPending}
        className="font-mono text-[10px] text-zinc-400 dark:text-zinc-500 w-5 flex-shrink-0 hover:text-[var(--ink-blue)] transition-colors"
        title="Reopen task"
        aria-label="Reopen task"
      >
        ✓
      </button>
      <span className="text-[13.5px] line-through text-meta truncate flex-1">
        {task.title}
      </span>
      {task.project_name && (
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded border flex-shrink-0 text-meta border-hair">
          {task.project_name}
        </span>
      )}
    </div>
  );
}

export function IntentSection() {
  const [showAddForm, setShowAddForm] = useState(false);
  const [addTitle, setAddTitle] = useState('');
  const [addPriority, setAddPriority] = useState<number>(3);
  const [showCompleted, setShowCompleted] = useState(false);
  const [intentText, setIntentText] = useState('');
  const intentRef = useRef<HTMLTextAreaElement>(null);

  const queryClient = useQueryClient();

  const { data, isError } = useQuery({
    queryKey: QUERY_KEYS.myDay.today(),
    queryFn: fetchMyDay,
    refetchInterval: 60_000,
  });

  // Intent query
  const { data: intentData, isError: isIntentError } = useQuery({
    queryKey: QUERY_KEYS.intent.today(),
    queryFn: fetchIntentToday,
  });

  // Seed local state from server (only when server value changes, not on user typing)
  useEffect(() => {
    setIntentText(intentData?.intent ?? '');
  }, [intentData?.intent]);

  // Intent mutation
  const intentMutation = useMutation({
    mutationFn: saveIntentToday,
    onError: () => toast.error('Failed to save intent'),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.intent.today() }),
  });

  // Debounced autosave (800 ms)
  useEffect(() => {
    if (intentText === (intentData?.intent ?? '')) return;
    const id = setTimeout(() => intentMutation.mutate(intentText), 800);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intentText, intentData?.intent]);

  // Auto-grow textarea
  useEffect(() => {
    const el = intentRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, [intentText]);

  const addMutation = useMutation({
    mutationFn: createQuickTask,
    onSuccess: () => {
      setAddTitle('');
      setAddPriority(3);
      setShowAddForm(false);
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() });
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

  useEffect(() => {
    if (completedToday.length === 0) setShowCompleted(false);
  }, [completedToday.length]);

  if (isError) {
    return (
      <section id="intent">
        <SectionHeader marker="Today's intent" />
        <p className="text-[12px] font-mono text-meta pl-5">Couldn't load tasks</p>
      </section>
    );
  }

  return (
    <section id="intent">
      <SectionHeader marker="Today's intent" />

      {/* Intent sub-query error state */}
      {isIntentError && (
        <p className="text-[11px] font-mono text-destructive pl-0 mb-2">Couldn't load saved intent</p>
      )}

      {/* Intent textarea — debounced autosave */}
      <textarea
        ref={intentRef}
        value={intentText}
        onChange={(e) => setIntentText(e.target.value)}
        rows={1}
        placeholder="Set today's intent…"
        maxLength={280}
        aria-label="Today's intent"
        className="w-full resize-none border-l-2 border-dashed border-hair bg-transparent px-3 py-1 mb-4 font-serif italic text-[18px] text-strong outline-none focus:border-solid focus:border-l-2 focus:border-[hsl(var(--ring))] placeholder:text-faint"
      />

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
              aria-label="Task title"
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
            <kbd className="font-mono text-[10px] px-1 py-0.5 rounded border border-hair bg-paper text-meta">+</kbd>
            add task
          </button>
        )}

        {/* Completed-today footer */}
        {completedToday.length > 0 && (
          <div className="mt-2 pt-2 border-t border-zinc-100 dark:border-zinc-800">
            <button
              onClick={() => setShowCompleted((v) => !v)}
              className="flex items-center gap-1 text-[11px] font-mono text-meta hover:text-soft transition-colors"
            >
              {showCompleted
                ? <><ChevronDown className="h-3 w-3" /> Hide completed</>
                : <><ChevronRight className="h-3 w-3" /> {completedToday.length} done today</>
              }
            </button>

            {showCompleted && (
              <div className="mt-1 space-y-0">
                {completedToday.map((task) => <CompletedRow key={task.id} task={task} />)}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { GradientProgressBar } from '@/components/my-day/primitives/GradientProgressBar';
import { fetchThreads, createThread, resumeThread } from '@/lib/api';
import type { Thread } from '@/types';
import { ErrorSentinel } from '@/components/shared/ErrorSentinel';

function formatLastAt(iso: string): string {
  try {
    const d = new Date(iso);
    const today = new Date();
    if (d.toDateString() === today.toDateString()) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch {
    return iso;
  }
}

function ThreadRow({ thread }: { thread: Thread }) {
  const queryClient = useQueryClient();
  const resumeMutation = useMutation({
    mutationFn: () => resumeThread(thread.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.threads() });
      toast.success('Thread resumed');
    },
    onError: (err: Error) => toast.error(`Couldn't resume: ${err.message}`),
  });

  const pct = Math.round((thread.progress ?? 0) * 100);

  return (
    <div className="group block border-l border-hair pl-5 py-1 hover:border-[var(--ink-blue,#0b3a8a)] transition-colors">
      <p className="text-[13.5px] text-strong leading-snug">{thread.title}</p>
      {thread.anchor && (
        <p className="mt-0.5 font-serif italic text-[12px] text-meta">↳ {thread.anchor}</p>
      )}
      <div className="mt-1.5 flex items-center gap-3">
        <div className="max-w-[140px] flex-1">
          <GradientProgressBar value={pct} color="var(--ink-blue, #0b3a8a)" />
        </div>
        <span className="font-mono text-[10px] text-faint tabular-nums">
          {pct}% · {formatLastAt(thread.last_at)}
        </span>
        <button
          type="button"
          onClick={() => resumeMutation.mutate()}
          disabled={resumeMutation.isPending}
          className="ml-auto font-mono text-[10px] text-[var(--ink-blue,#0b3a8a)] opacity-60 group-hover:opacity-100 hover:underline transition-opacity disabled:opacity-40"
        >
          resume →
        </button>
      </div>
    </div>
  );
}

/**
 * § Open threads — user-created + auto-seeded resumable lines of work
 * (prototype :236-256). Hidden when no open threads, but always offers an
 * inline create affordance via the section header.
 */
export function ThreadsSection() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState('');
  const [anchor, setAnchor] = useState('');

  const { data, isError } = useQuery<Thread[]>({
    queryKey: QUERY_KEYS.myDay.threads(),
    queryFn: fetchThreads,
    staleTime: 60_000,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createThread({ title: title.trim(), anchor: anchor.trim() || null }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.threads() });
      setTitle('');
      setAnchor('');
      setAdding(false);
      toast.success('Thread created');
    },
    onError: (err: Error) => toast.error(`Couldn't create thread: ${err.message}`),
  });

  const threads = (data ?? []).filter((t) => t.status === 'open');

  if (isError) return (
    <ErrorSentinel message="Unable to load threads." />
  );
  if (threads.length === 0 && !adding) {
    return (
      <section id="threads">
        <SectionHeader
          marker="Open threads"
          right={
            <button
              type="button"
              onClick={() => setAdding(true)}
              className="font-mono text-[10px] text-faint hover:text-strong transition-colors"
            >
              + new thread
            </button>
          }
        />
        <p className="pl-5 text-[12px] font-mono text-faint">
          No open threads — start one to track a mid-flight line of work.
        </p>
      </section>
    );
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || createMutation.isPending) return;
    createMutation.mutate();
  };

  return (
    <section id="threads">
      <SectionHeader
        marker="Open threads"
        meta={threads.length > 0 ? `${threads.length} mid-flight` : undefined}
        right={
          !adding ? (
            <button
              type="button"
              onClick={() => setAdding(true)}
              className="font-mono text-[10px] text-faint hover:text-strong transition-colors"
            >
              + new thread
            </button>
          ) : undefined
        }
      />

      {adding && (
        <form
          onSubmit={handleSubmit}
          className="mb-4 space-y-2 border-l border-dashed border-hair pl-5"
        >
          <input
            autoFocus
            aria-label="Thread title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                setAdding(false);
                setTitle('');
                setAnchor('');
              }
            }}
            placeholder="What are you mid-flight on?"
            className="w-full bg-transparent text-[13.5px] text-strong outline-none placeholder:text-faint"
            disabled={createMutation.isPending}
          />
          <input
            aria-label="Thread anchor"
            value={anchor}
            onChange={(e) => setAnchor(e.target.value)}
            placeholder="anchor (optional) — e.g. notebook §4.2"
            className="w-full bg-transparent font-serif italic text-[12px] text-meta outline-none placeholder:text-faint"
            disabled={createMutation.isPending}
          />
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={createMutation.isPending || !title.trim()}
              className="font-mono text-[11px] text-[var(--ink-blue,#0b3a8a)] hover:opacity-70 disabled:opacity-30"
            >
              create
            </button>
            <button
              type="button"
              onClick={() => {
                setAdding(false);
                setTitle('');
                setAnchor('');
              }}
              className="font-mono text-[11px] text-faint hover:text-soft"
            >
              cancel
            </button>
          </div>
        </form>
      )}

      <div className="space-y-3.5">
        {threads.map((t) => (
          <ThreadRow key={t.id} thread={t} />
        ))}
      </div>
    </section>
  );
}

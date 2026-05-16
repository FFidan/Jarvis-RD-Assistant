import { useEffect, useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { HeroPulse } from './HeroPulse';
import { HeroTask } from './HeroTask';
import { HeroThread } from './HeroThread';
import { HeroResumeReading } from './HeroResumeReading';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchFeed, fetchThreads } from '@/lib/api';
import type { FeedResponse, Thread } from '@/types';

/**
 * Hero "Now" mode. Persisted to localStorage('myday.heroMode') so the user's
 * focus choice survives reload (prototype uses local state; the v3 design
 * adds persistence). Kept local to My-Day — not in the shared ui-store.
 */
type Mode = 'pulse' | 'thread' | 'task' | 'reading';

const STORAGE_KEY = 'myday.heroMode';

function readStoredMode(): Mode | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'pulse' || v === 'thread' || v === 'task' || v === 'reading') return v;
  } catch {
    /* ignore */
  }
  return null;
}

interface ModePickerProps {
  mode: Mode;
  onChange: (m: Mode) => void;
  hasThread: boolean;
  hasTask: boolean;
  hasReading: boolean;
  taskPhaseLabel: string;
}

function ModePicker({ mode, onChange, hasThread, hasTask, hasReading, taskPhaseLabel }: ModePickerProps) {
  const tabs: { value: Mode; label: string; show: boolean }[] = [
    { value: 'pulse', label: 'Pulse #1', show: true },
    { value: 'thread', label: 'Resume thread', show: hasThread },
    { value: 'task', label: taskPhaseLabel, show: hasTask },
    { value: 'reading', label: 'Resume reading', show: hasReading },
  ];
  const visibleTabs = tabs.filter((t) => t.show);
  return (
    <div
      role="tablist"
      aria-label="Now mode"
      className="bg-zinc-100/80 dark:bg-zinc-800/60 rounded-md p-0.5 flex gap-0.5"
    >
      {visibleTabs.map((t) => (
        <button
          key={t.value}
          role="tab"
          aria-selected={mode === t.value}
          data-state={mode === t.value ? 'active' : 'inactive'}
          tabIndex={mode === t.value ? 0 : -1}
          onKeyDown={(e) => {
            if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
              e.preventDefault();
              const idx = visibleTabs.findIndex((tab) => tab.value === mode);
              const next =
                (idx + (e.key === 'ArrowRight' ? 1 : -1) + visibleTabs.length) % visibleTabs.length;
              const nextTab = visibleTabs[next];
              if (nextTab) onChange(nextTab.value);
            }
          }}
          onClick={() => onChange(t.value)}
          className={`h-6 px-2.5 rounded text-[10.5px] font-mono transition-colors ${
            mode === t.value ? 'bg-card text-strong shadow-sm' : 'text-meta hover:text-soft'
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

function taskLabelForPhase(phase: string): string {
  if (phase === 'short-break' || phase === 'long-break') return 'On break';
  return 'Continue task';
}

export function HeroNow() {
  const phase = usePomodoroStore((s) => s.phase);
  const hasTask = phase !== 'idle';
  const taskPhaseLabel = taskLabelForPhase(phase);

  const { data: readingData } = useQuery<FeedResponse>({
    queryKey: ['feed', 'reading', 'hero'],
    queryFn: () => fetchFeed({ view: 'library', filter: 'reading', limit: 20 }),
    staleTime: 60_000,
  });
  const hasReading = (readingData?.papers ?? []).some((p) => p.state === 'reading');

  const { data: threadData } = useQuery<Thread[]>({
    queryKey: ['my-day', 'threads'],
    queryFn: fetchThreads,
    staleTime: 60_000,
  });
  const hasThread = (threadData ?? []).some((t) => t.status === 'open');

  // Smart default (SPEC §4): paused-Pomodoro → task; an open thread → thread;
  // else pulse. A persisted explicit choice always wins.
  const [mode, setMode] = useState<Mode>(() => {
    const stored = readStoredMode();
    if (stored) return stored;
    return 'pulse';
  });

  // Apply the smart default once data has loaded and the user has no stored
  // preference yet.
  useEffect(() => {
    if (readStoredMode()) return;
    if (hasTask) setMode('task');
    else if (hasThread) setMode('thread');
  }, [hasTask, hasThread]);

  const persistMode = useCallback((next: Mode) => {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
  }, []);

  const handleChange = useCallback(
    (next: Mode) => {
      setMode(next);
      persistMode(next);
    },
    [persistMode],
  );

  // If the active mode's data disappears, fall back to pulse (do not persist —
  // it's an availability fallback, not an explicit user choice).
  useEffect(() => {
    if (mode === 'task' && !hasTask) setMode('pulse');
    if (mode === 'thread' && !hasThread) setMode('pulse');
    if (mode === 'reading' && !hasReading) setMode('pulse');
  }, [mode, hasTask, hasThread, hasReading]);

  return (
    <section id="now">
      <SectionHeader
        marker="Now"
        right={
          <ModePicker
            mode={mode}
            onChange={handleChange}
            hasThread={hasThread}
            hasTask={hasTask}
            hasReading={hasReading}
            taskPhaseLabel={taskPhaseLabel}
          />
        }
      />

      {/* Hero card chrome */}
      <div className="rounded-xl border border-[var(--ink-blue-border,rgba(11,58,138,0.15))] bg-gradient-to-br from-[var(--surface-cream,#fdf9f0)] via-[var(--surface-card,#ffffff)] to-[var(--surface-cool,#f5f8fe)] p-7 relative overflow-hidden shadow-[0_1px_0_rgba(0,0,0,0.02)]">
        {/* Top-right ink-blue blob */}
        <div className="absolute -top-12 -right-12 h-40 w-40 rounded-full bg-[var(--ink-blue-soft,rgba(11,58,138,0.05))] blur-3xl pointer-events-none" />
        {/* Bottom-left amber blob */}
        <div className="absolute -bottom-12 -left-12 h-40 w-40 rounded-full bg-amber-100/40 blur-3xl pointer-events-none" />

        <div className="relative">
          {mode === 'pulse' && <HeroPulse />}
          {mode === 'thread' && <HeroThread />}
          {mode === 'task' && <HeroTask />}
          {mode === 'reading' && <HeroResumeReading />}
        </div>
      </div>
    </section>
  );
}

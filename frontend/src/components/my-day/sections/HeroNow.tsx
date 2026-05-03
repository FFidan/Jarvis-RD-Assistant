import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { SectionHeader } from './SectionHeader';
import { HeroPulse } from './HeroPulse';
import { HeroTask } from './HeroTask';
import { HeroResumeReading } from './HeroResumeReading';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchFeed } from '@/lib/api';
import type { FeedResponse } from '@/types';

type HeroMode = 'pulse' | 'task' | 'reading';

const STORAGE_KEY = 'myday.heroMode';

function isHeroMode(value: string): value is HeroMode {
  return value === 'pulse' || value === 'task' || value === 'reading';
}

interface ModePickerProps {
  mode: HeroMode;
  onChange: (m: HeroMode) => void;
  hasTask: boolean;
  hasReading: boolean;
}

function ModePicker({ mode, onChange, hasTask, hasReading }: ModePickerProps) {
  const tabs: { value: HeroMode; label: string; show: boolean }[] = [
    { value: 'pulse', label: 'Pulse #1', show: true },
    { value: 'task', label: 'Continue task', show: hasTask },
    { value: 'reading', label: 'Resume reading', show: hasReading },
  ];
  return (
    <div className="bg-zinc-100/80 dark:bg-zinc-800/60 rounded-md p-0.5 flex gap-0.5">
      {tabs.filter((t) => t.show).map((t) => (
        <button
          key={t.value}
          onClick={() => onChange(t.value)}
          className={`h-6 px-2.5 rounded text-[10.5px] font-mono transition-colors ${
            mode === t.value
              ? 'bg-card text-strong shadow-sm'
              : 'text-meta hover:text-soft'
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function HeroNow() {
  const [mode, setMode] = useState<HeroMode>('pulse');
  const attachedItem = usePomodoroStore((s) => s.attachedItem);
  const phase = usePomodoroStore((s) => s.phase);
  const hasTask = phase !== 'idle' && attachedItem !== null;

  const { data: readingData } = useQuery<FeedResponse>({
    queryKey: ['feed', 'reading', 'hero'],
    queryFn: () => fetchFeed({ view: 'library', filter: 'reading', limit: 20 }),
    staleTime: 60_000,
  });

  const hasReading = (readingData?.papers ?? []).some((p) => p.state === 'reading');

  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved !== null) {
        const validMode = isHeroMode(saved) ? saved : 'pulse';
        setMode(validMode);
      }
    } catch {
      /* ignore — blocked storage or SSR */
    }
  }, []);

  // If task tab disappears (Pomodoro ended), fall back to pulse
  useEffect(() => {
    if (!hasTask && mode === 'task') setMode('pulse');
  }, [hasTask, mode]);

  // If reading tab disappears, fall back to pulse
  useEffect(() => {
    if (!hasReading && mode === 'reading') setMode('pulse');
  }, [hasReading, mode]);

  const handleChange = (next: HeroMode) => {
    setMode(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
  };

  return (
    <section id="now">
      <SectionHeader
        marker="Now"
        right={
          <ModePicker
            mode={mode}
            onChange={handleChange}
            hasTask={hasTask}
            hasReading={hasReading}
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
          {mode === 'task' && <HeroTask />}
          {mode === 'reading' && <HeroResumeReading />}
        </div>
      </div>
    </section>
  );
}

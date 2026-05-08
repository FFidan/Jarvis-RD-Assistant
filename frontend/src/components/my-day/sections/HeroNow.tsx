import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { HeroPulse } from './HeroPulse';
import { HeroTask } from './HeroTask';
import { HeroResumeReading } from './HeroResumeReading';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { useUIStore, type HeroMode } from '@/stores/ui-store';
import { fetchFeed } from '@/lib/api';
import type { FeedResponse } from '@/types';

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
    <div role="tablist" aria-label="Now mode" className="bg-zinc-100/80 dark:bg-zinc-800/60 rounded-md p-0.5 flex gap-0.5">
      {tabs.filter((t) => t.show).map((t) => {
        const visibleTabs = tabs.filter((tab) => tab.show);
        return (
          <button
            key={t.value}
            role="tab"
            aria-selected={mode === t.value}
            tabIndex={mode === t.value ? 0 : -1}
            onKeyDown={(e) => {
              if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                e.preventDefault();
                const idx = visibleTabs.findIndex((tab) => tab.value === mode);
                const next = (idx + (e.key === 'ArrowRight' ? 1 : -1) + visibleTabs.length) % visibleTabs.length;
                const nextTab = visibleTabs[next];
                if (nextTab) onChange(nextTab.value);
              }
            }}
            onClick={() => onChange(t.value)}
            className={`h-6 px-2.5 rounded text-[10.5px] font-mono transition-colors ${
              mode === t.value
                ? 'bg-card text-strong shadow-sm'
                : 'text-meta hover:text-soft'
            }`}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

export function HeroNow() {
  const mode = useUIStore((s) => s.heroMode);
  const setHeroMode = useUIStore((s) => s.setHeroMode);
  const phase = usePomodoroStore((s) => s.phase);
  const hasTask = phase !== 'idle';

  const { data: readingData } = useQuery<FeedResponse>({
    queryKey: ['feed', 'reading', 'hero'],
    queryFn: () => fetchFeed({ view: 'library', filter: 'reading', limit: 20 }),
    staleTime: 60_000,
  });

  const hasReading = (readingData?.papers ?? []).some((p) => p.state === 'reading');

  // If task tab disappears (Pomodoro ended), fall back to pulse
  useEffect(() => {
    if (!hasTask && mode === 'task') setHeroMode('pulse');
  }, [hasTask, mode, setHeroMode]);

  // If reading tab disappears, fall back to pulse
  useEffect(() => {
    if (!hasReading && mode === 'reading') setHeroMode('pulse');
  }, [hasReading, mode, setHeroMode]);

  const handleChange = (next: HeroMode) => {
    setHeroMode(next);
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

import { useEffect, useState } from 'react';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
} from '@/components/ui/tooltip';
import { SectionHeader } from './SectionHeader';
import { HeroPulse } from './HeroPulse';
import { HeroTask } from './HeroTask';
import { HeroResumeReading } from './HeroResumeReading';

type HeroMode = 'pulse' | 'resume' | 'task';

const STORAGE_KEY = 'myday.heroMode';

function isHeroMode(value: string): value is HeroMode {
  return value === 'pulse' || value === 'resume' || value === 'task';
}

export function HeroNow() {
  const [mode, setMode] = useState<HeroMode>('pulse');

  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved !== null && isHeroMode(saved)) setMode(saved);
    } catch {
      /* ignore — blocked storage or SSR */
    }
  }, []);

  const handleChange = (next: string) => {
    if (!isHeroMode(next)) return;
    setMode(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
  };

  return (
    <section id="now">
      <SectionHeader marker="Now" />
      <Tabs value={mode} onValueChange={handleChange}>
        <TabsList className="mb-3">
          <TabsTrigger value="pulse">Pulse #1</TabsTrigger>

          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                {/* span needed so Tooltip can attach to a disabled element */}
                <span>
                  <TabsTrigger value="resume" disabled>
                    Resume reading
                  </TabsTrigger>
                </span>
              </TooltipTrigger>
              <TooltipContent>Coming in Phase 2</TooltipContent>
            </Tooltip>
          </TooltipProvider>

          <TabsTrigger value="task">Continue task</TabsTrigger>
        </TabsList>

        {/* Hero card chrome */}
        <div className="rounded-xl border border-[var(--ink-blue-border,rgba(11,58,138,0.15))] bg-gradient-to-br from-[var(--surface-cream,#fdf9f0)] via-[var(--surface-card,#ffffff)] to-[var(--surface-cool,#f5f8fe)] p-7 relative overflow-hidden shadow-[0_1px_0_rgba(0,0,0,0.02)]">
          {/* Top-right ink-blue blob */}
          <div className="absolute -top-12 -right-12 h-40 w-40 rounded-full bg-[var(--ink-blue-soft,rgba(11,58,138,0.05))] blur-3xl pointer-events-none" />
          {/* Bottom-left amber blob */}
          <div className="absolute -bottom-12 -left-12 h-40 w-40 rounded-full bg-amber-100/40 blur-3xl pointer-events-none" />

          <TabsContent value="pulse" className="relative mt-0">
            <HeroPulse />
          </TabsContent>
          <TabsContent value="resume" className="relative mt-0">
            <HeroResumeReading />
          </TabsContent>
          <TabsContent value="task" className="relative mt-0">
            <HeroTask />
          </TabsContent>
        </div>
      </Tabs>
    </section>
  );
}

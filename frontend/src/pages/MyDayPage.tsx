import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { DateMasthead } from '@/components/my-day/sections/DateMasthead';
// YesterdaySection removed (W3-6); re-add when daily-rollup job ships
import { HeroNow } from '@/components/my-day/sections/HeroNow';
import { IntentSection } from '@/components/my-day/sections/IntentSection';
import { ProjectsSection } from '@/components/my-day/sections/ProjectsSection';
import { TodaysPulseSection } from '@/components/my-day/sections/TodaysPulseSection';
import { TriageSection } from '@/components/my-day/sections/TriageSection';
import { LearningFocusSection } from '@/components/my-day/sections/LearningFocusSection';
import { WeeklyDigestSection } from '@/components/my-day/sections/WeeklyDigestSection';
import { EndOfDaySection } from '@/components/my-day/sections/EndOfDaySection';
import { MyDayFooter } from '@/components/my-day/sections/MyDayFooter';

export function MyDayPage() {
  const entryNum = Math.floor((Date.now() - new Date('2026-01-01').getTime()) / 86400000);
  const { hash } = useLocation();

  useEffect(() => {
    if (!hash) return;
    const id = hash.slice(1);
    let frames = 0;
    let raf = 0;
    const tryScroll = () => {
      const el = document.getElementById(id);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      if (++frames < 30) raf = requestAnimationFrame(tryScroll);
    };
    raf = requestAnimationFrame(tryScroll);
    return () => cancelAnimationFrame(raf);
  }, [hash]);

  return (
    <div className="bg-paper min-h-screen">
      <main className="max-w-page mx-auto px-10 py-10 space-y-12">
        <DateMasthead />
        {/* YesterdaySection removed (W3-6); re-add when daily-rollup job ships */}
        <HeroNow />
        <IntentSection />
        <ProjectsSection />
        <TodaysPulseSection />
        <TriageSection />
        <LearningFocusSection />
        <WeeklyDigestSection />
        <EndOfDaySection />
        <MyDayFooter entryNum={entryNum} />
      </main>
    </div>
  );
}

import { DateMasthead } from '@/components/my-day/sections/DateMasthead';
import { YesterdaySection } from '@/components/my-day/sections/YesterdaySection';
import { HeroNow } from '@/components/my-day/sections/HeroNow';
import { IntentSection } from '@/components/my-day/sections/IntentSection';
import { ProjectsSection } from '@/components/my-day/sections/ProjectsSection';
import { TodaysPulseSection } from '@/components/my-day/sections/TodaysPulseSection';
import { TriageSection } from '@/components/my-day/sections/TriageSection';
import { LearningFocusSection } from '@/components/my-day/sections/LearningFocusSection';
import { EndOfDaySection } from '@/components/my-day/sections/EndOfDaySection';

export function MyDayPage() {
  return (
    <div className="bg-paper min-h-screen">
      <main className="max-w-page mx-auto px-10 py-10 space-y-12">
        <DateMasthead />
        <YesterdaySection />
        <HeroNow />
        <IntentSection />
        <ProjectsSection />
        <TodaysPulseSection />
        <TriageSection />
        <LearningFocusSection />
        <EndOfDaySection />
      </main>
    </div>
  );
}

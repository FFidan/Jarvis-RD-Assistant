import { useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DayHeader } from '@/components/my-day/DayHeader';
import { PulsePreviewCard } from '@/components/my-day/PulsePreviewCard';
import { PomodoroTimer } from '@/components/my-day/PomodoroTimer';
import { QuickAddTask } from '@/components/my-day/QuickAddTask';
import { TaskList } from '@/components/my-day/TaskList';
import { ActionItemsCard } from '@/components/my-day/ActionItemsCard';
import { LearningCardsSummary } from '@/components/my-day/LearningCardsSummary';
import { ProjectPulse } from '@/components/my-day/ProjectPulse';
import { fetchMyDay } from '@/lib/api';
import type { MyDayResponse } from '@/types';

export function MyDayPage() {
  const pulseCardRef = useRef<HTMLDivElement | null>(null);
  const focusRef = useRef<HTMLDivElement | null>(null);

  const { data, isLoading, isError } = useQuery<MyDayResponse>({
    queryKey: ['my-day'],
    queryFn: fetchMyDay,
    refetchInterval: 60_000,
  });

  return (
    <div className="space-y-6 max-w-4xl mx-auto">
      {/* Triage header with counter strip */}
      <DayHeader pulseCardRef={pulseCardRef} focusRef={focusRef} />

      {/* Pulse preview */}
      <PulsePreviewCard containerRef={pulseCardRef} />

      {/* Focus + Tasks row */}
      <section ref={focusRef} className="grid gap-6 md:grid-cols-2">
        <PomodoroTimer
          todayFocusHours={data?.today_focus_hours ?? 0}
          focusStreakDays={data?.focus_streak_days ?? 0}
        />

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-lg">Today&apos;s Tasks</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <QuickAddTask />
            {isLoading && (
              <p className="text-sm text-muted-foreground text-center py-4">Loading tasks…</p>
            )}
            {isError && (
              <p className="text-sm text-destructive text-center py-4">Failed to load tasks.</p>
            )}
            {data && <TaskList tasks={data.tasks} />}
          </CardContent>
        </Card>
      </section>

      {/* Action items triage */}
      <ActionItemsCard />

      {/* Learning + Project pulse row */}
      <section className="grid gap-6 md:grid-cols-2">
        <LearningCardsSummary />
        {data && <ProjectPulse projects={data.project_pulse} />}
      </section>
    </div>
  );
}

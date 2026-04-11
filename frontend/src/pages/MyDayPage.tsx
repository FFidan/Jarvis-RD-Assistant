import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { PomodoroTimer } from '@/components/my-day/PomodoroTimer';
import { PulseDeck } from '@/components/my-day/PulseDeck';
import { QuickAddTask } from '@/components/my-day/QuickAddTask';
import { TaskList } from '@/components/my-day/TaskList';
import { ProjectPulse } from '@/components/my-day/ProjectPulse';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { fetchMyDay } from '@/lib/api';
import type { MyDayResponse } from '@/types';

export function MyDayPage() {
  const todayStr = new Intl.DateTimeFormat('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  }).format(new Date());

  const { data, isLoading, isError } = useQuery<MyDayResponse>({
    queryKey: ['my-day'],
    queryFn: fetchMyDay,
    refetchInterval: 60000,
  });

  const pomodoroPhase = usePomodoroStore(s => s.phase);
  const pomodoroStartWork = usePomodoroStore(s => s.startWork);

  if (isLoading) {
    return (
      <div className="space-y-6 max-w-4xl mx-auto">
        <Skeleton className="h-10 w-48" />
        <Skeleton className="h-64 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="max-w-4xl mx-auto p-8 text-center">
        <p className="text-destructive">Failed to load My Day data.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-4xl mx-auto">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold">My Day</h1>
        <p className="text-muted-foreground">{todayStr}</p>
      </div>

      {/* Today's Pulse deck */}
      <PulseDeck />

      {/* Pomodoro Timer */}
      <PomodoroTimer
        todayFocusHours={data.today_focus_hours}
        focusStreakDays={data.focus_streak_days}
      />

      {/* Today's Tasks */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-lg">Today&apos;s Tasks</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <QuickAddTask />
          <TaskList tasks={data.tasks} />
        </CardContent>
      </Card>

      {/* Learning + Recommendations row */}
      {data.cards_due === 0 && data.recommendations.length === 0 ? (
        <div className="flex items-center justify-center gap-4 py-3 text-sm text-muted-foreground border rounded-lg">
          <span>No reviews or recommendations right now</span>
          <Link to="/cards" className="text-primary hover:underline">Learning Cards →</Link>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {/* Learning Engine */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-lg">Learning</CardTitle>
            </CardHeader>
            <CardContent>
              {data.cards_due > 0 ? (
                <div className="flex items-center justify-between p-4 bg-orange-50 dark:bg-orange-950/30 border border-orange-100 dark:border-orange-900 rounded-lg">
                  <div>
                    <p className="font-bold text-orange-800 dark:text-orange-300">
                      {data.cards_due} cards due
                    </p>
                    <p className="text-xs text-orange-600 dark:text-orange-400">
                      Review to maintain your streaks.
                    </p>
                  </div>
                  <Button asChild size="sm" className="bg-orange-600 hover:bg-orange-700">
                    <Link to="/cards">Review Now</Link>
                  </Button>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground text-center py-4">
                  No reviews pending.
                </p>
              )}
            </CardContent>
          </Card>

          {/* Recommended Papers */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-lg">Recommended</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {data.recommendations.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-4">
                  No recommendations ready.
                </p>
              ) : (
                data.recommendations.map((rec) => (
                  <div
                    key={rec.recommendation_id}
                    className="p-3 rounded-lg border hover:bg-muted/50 transition-colors flex flex-col gap-2"
                  >
                    <p className="font-medium text-sm leading-tight line-clamp-2" title={rec.title}>
                      {rec.title}
                    </p>
                    <div className="flex justify-between items-center">
                      <p className="text-xs text-muted-foreground">
                        Match: {(rec.score * 100).toFixed(0)}%
                      </p>
                      <Button
                        variant="secondary"
                        size="sm"
                        className="h-7 text-xs"
                        disabled={pomodoroPhase !== 'idle'}
                        onClick={() =>
                          pomodoroStartWork({
                            id: rec.paper_id,
                            title: rec.title,
                            type: 'paper',
                          })
                        }
                      >
                        Focus &amp; Read
                      </Button>
                    </div>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Project Pulse */}
      <ProjectPulse projects={data.project_pulse} />
    </div>
  );
}

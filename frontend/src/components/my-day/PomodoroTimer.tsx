import { useEffect, useCallback } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { logFocusSession } from '@/lib/api';

interface PomodoroTimerProps {
  todayFocusHours: number;
  focusStreakDays: number;
}

/** Format focus hours for display: "0m", "12m", or "1.5h" */
const formatFocusTime = (hours: number) => {
  if (hours < 1 / 60) return '0m';
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  return `${hours.toFixed(1)}h`;
};

export function PomodoroTimer({ todayFocusHours, focusStreakDays }: PomodoroTimerProps) {
  // Individual selectors — prevents interval recreation on unrelated state changes (Bug #5)
  const phase = usePomodoroStore(s => s.phase);
  const secondsRemaining = usePomodoroStore(s => s.secondsRemaining);
  const cyclesCompleted = usePomodoroStore(s => s.cyclesCompleted);
  const targetCycles = usePomodoroStore(s => s.targetCycles);
  const workMinutes = usePomodoroStore(s => s.workMinutes);
  const pausedAt = usePomodoroStore(s => s.pausedAt);
  const attachedItem = usePomodoroStore(s => s.attachedItem);
  const completedSession = usePomodoroStore(s => s.completedSession);
  const startWork = usePomodoroStore(s => s.startWork);
  const pause = usePomodoroStore(s => s.pause);
  const resume = usePomodoroStore(s => s.resume);
  const skipBreak = usePomodoroStore(s => s.skipBreak);
  const stopAndLog = usePomodoroStore(s => s.stopAndLog);
  const clearCompletedSession = usePomodoroStore(s => s.clearCompletedSession);

  const queryClient = useQueryClient();

  // Focus log mutation
  const logMutation = useMutation({
    mutationFn: logFocusSession,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['my-day'] }),
  });

  // Recover wall-clock time after page refresh
  useEffect(() => {
    const s = usePomodoroStore.getState();
    if (s.phase === 'idle' || !s.startedAt) return;
    if (s.pausedAt) {
      // Paused: compute remaining without advancing clock
      const elapsed = s.pausedAt - s.startedAt - s.totalPausedMs;
      const remaining = Math.max(0, Math.ceil((s.phaseDurationMs - elapsed) / 1000));
      usePomodoroStore.setState({ secondsRemaining: remaining });
    } else {
      s.tick();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Run once on mount

  // Auto-log completed sessions (Bug #1)
  useEffect(() => {
    if (!completedSession) return;
    logMutation.mutate({
      duration_hours: completedSession.durationSeconds / 3600,
      task_id: completedSession.taskId,
      paper_id: completedSession.paperId,
    });
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification('Pomodoro Complete!', { body: 'Time for a break.' });
    }
    clearCompletedSession();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [completedSession, clearCompletedSession]);

  // Request notification permission on first Start Focus (Bug #7)
  const handleStartFocus = useCallback(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }
    startWork();
  }, [startWork]);

  // Stop timer and log partial session
  const handleStopAndLog = useCallback(() => {
    const result = stopAndLog();
    if (result && result.durationSeconds > 0) {
      logMutation.mutate({
        duration_hours: result.durationSeconds / 3600,
        task_id: result.taskId,
        paper_id: result.paperId,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stopAndLog]);

  // Format seconds to MM:SS
  const minutes = Math.floor(secondsRemaining / 60);
  const seconds = secondsRemaining % 60;
  const timeDisplay = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;

  // Phase label — shows "Paused" when paused (Bug #8)
  const phaseLabel = pausedAt ? 'Paused' : {
    idle: 'Ready',
    work: 'Focus Time',
    'short-break': 'Short Break',
    'long-break': 'Long Break',
  }[phase];

  // Phase colors
  const phaseColor = pausedAt ? 'text-yellow-500' : {
    idle: 'text-muted-foreground',
    work: 'text-red-500',
    'short-break': 'text-green-500',
    'long-break': 'text-blue-500',
  }[phase];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-lg">Pomodoro Timer</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Phase + Timer */}
        <div className="text-center space-y-2">
          <p className={`text-sm font-medium ${phaseColor}`}>{phaseLabel}</p>
          <p className="text-5xl font-mono font-bold tabular-nums tracking-tight">
            {phase === 'idle' ? `${String(workMinutes).padStart(2, '0')}:00` : timeDisplay}
          </p>

          {/* Cycle dots */}
          <div className="flex justify-center gap-1.5">
            {Array.from({ length: targetCycles }).map((_, i) => (
              <div
                key={i}
                className={`w-2.5 h-2.5 rounded-full ${
                  i < cyclesCompleted ? 'bg-primary' : 'bg-muted'
                }`}
              />
            ))}
            {phase !== 'idle' && (
              <span className="text-xs text-muted-foreground ml-2">
                Cycle {Math.min(cyclesCompleted + 1, targetCycles)}/{targetCycles}
              </span>
            )}
          </div>
        </div>

        {/* Attached item */}
        {attachedItem && (
          <p className="text-center text-sm text-muted-foreground truncate">
            Working on: {attachedItem.title}
          </p>
        )}

        {/* Action buttons (Bug #11 fix — Pause/Resume during work, Skip during break) */}
        <div className="flex justify-center gap-2">
          {phase === 'idle' && (
            <Button onClick={handleStartFocus}>Start Focus</Button>
          )}
          {phase === 'work' && !pausedAt && (
            <>
              <Button variant="outline" onClick={pause}>Pause</Button>
              <Button variant="destructive" onClick={handleStopAndLog}>
                Stop &amp; Log
              </Button>
            </>
          )}
          {phase === 'work' && pausedAt && (
            <>
              <Button onClick={resume}>Resume</Button>
              <Button variant="destructive" onClick={handleStopAndLog}>
                Stop &amp; Log
              </Button>
            </>
          )}
          {(phase === 'short-break' || phase === 'long-break') && (
            <>
              <Button variant="outline" onClick={skipBreak}>Skip Break</Button>
              <Button variant="destructive" onClick={handleStopAndLog}>
                Stop &amp; Log
              </Button>
            </>
          )}
        </div>

        {/* Stats footer (Bug #9 fix — better focus time formatting) */}
        <div className="flex justify-center gap-4 text-sm text-muted-foreground pt-2 border-t">
          <span>Today: {formatFocusTime(todayFocusHours)} focused</span>
          <span>|</span>
          <span>Streak: {focusStreakDays} {focusStreakDays === 1 ? 'day' : 'days'}</span>
        </div>
      </CardContent>
    </Card>
  );
}

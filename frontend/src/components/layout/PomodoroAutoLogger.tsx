import { useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { logFocusSession } from '@/lib/api';

export function PomodoroAutoLogger() {
  const completedSession = usePomodoroStore(s => s.completedSession);
  const clearCompletedSession = usePomodoroStore(s => s.clearCompletedSession);
  const queryClient = useQueryClient();

  const logMutation = useMutation({
    mutationFn: logFocusSession,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() }),
  });

  // Recover wall-clock time after page refresh
  useEffect(() => {
    const s = usePomodoroStore.getState();
    if (s.phase === 'idle' || !s.startedAt) return;
    if (s.pausedAt) {
      const elapsed = s.pausedAt - s.startedAt - s.totalPausedMs;
      const remaining = Math.max(0, Math.ceil((s.phaseDurationMs - elapsed) / 1000));
      usePomodoroStore.setState({ secondsRemaining: remaining });
    } else {
      s.tick();
    }
  }, []); // once on mount — eslint-disable-line react-hooks/exhaustive-deps

  // Auto-log completed sessions
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
  }, [completedSession, clearCompletedSession]); // eslint-disable-line react-hooks/exhaustive-deps

  return null;
}

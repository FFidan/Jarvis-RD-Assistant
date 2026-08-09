import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { QUERY_KEYS } from '@/lib/query-keys';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import {
  completeFocusSession,
  fetchActiveFocusSession,
  pauseFocusSession,
  resumeFocusSession,
  startFocusSession,
} from '@/lib/api';

export function PomodoroAutoLogger() {
  const completedSession = usePomodoroStore(s => s.completedSession);
  const clearCompletedSession = usePomodoroStore(s => s.clearCompletedSession);
  const pendingOperation = usePomodoroStore(s => s.pendingOperation);
  const applyServerSession = usePomodoroStore(s => s.applyServerSession);
  const clearPendingOperation = usePomodoroStore(s => s.clearPendingOperation);
  const queryClient = useQueryClient();

  // Rehydrate Telegram- or Web-started intervals and observe cross-client
  // transitions. Preferences and break cycles remain local.
  useEffect(() => {
    let disposed = false;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || usePomodoroStore.getState().pendingOperation !== null) return;
      inFlight = true;
      try {
        const session = await fetchActiveFocusSession();
        if (!disposed) applyServerSession(session);
      } catch {
        // A transient refresh failure must not erase a locally rendered
        // server snapshot. The next poll retries without user-facing noise.
      } finally {
        inFlight = false;
      }
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 10_000);
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      disposed = true;
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [applyServerSession]);

  useEffect(() => {
    if (pendingOperation === null) return;
    let disposed = false;
    const operation = pendingOperation;

    const run = async () => {
      try {
        if (operation.kind === 'start') {
          const session = await startFocusSession({
            duration_seconds: operation.durationSeconds,
            source: 'web',
            ...(operation.taskId === undefined ? {} : { task_id: operation.taskId }),
            ...(operation.paperId === undefined ? {} : { paper_id: operation.paperId }),
          });
          if (!disposed) applyServerSession(session);
        } else if (operation.kind === 'pause') {
          const result = await pauseFocusSession(operation.sessionId);
          if (!disposed) applyServerSession(result.session);
        } else if (operation.kind === 'resume') {
          const result = await resumeFocusSession(operation.sessionId);
          if (!disposed) applyServerSession(result.session);
        } else {
          const result = await completeFocusSession(operation.sessionId, operation.mode);
          if (!disposed) {
            applyServerSession(result.session);
            if (result.changed) {
              void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.myDay.today() });
            }
          }
        }
      } catch {
        if (!disposed) {
          clearPendingOperation(operation.id);
          try {
            applyServerSession(await fetchActiveFocusSession());
          } catch {
            applyServerSession(null);
          }
          toast.error('The focus session could not be synchronized. The server state was restored.');
        }
        return;
      }
      if (!disposed) clearPendingOperation(operation.id);
    };

    void run();
    return () => { disposed = true; };
  }, [applyServerSession, clearPendingOperation, pendingOperation, queryClient]);

  // Completion accounting is server-owned. This signal only drives the local
  // browser notification after the server confirms completion.
  useEffect(() => {
    if (!completedSession) return;
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification('Pomodoro Complete!', { body: 'Time for a break.' });
    }
    clearCompletedSession();
  }, [completedSession, clearCompletedSession]);

  return null;
}

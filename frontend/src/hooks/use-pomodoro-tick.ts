import { useEffect } from 'react';
import { usePomodoroStore } from '@/stores/pomodoro-store';

/**
 * Global tick driver for the Pomodoro timer.
 * Mount once (in AppShell) so the interval survives navigation.
 * PomodoroTimer.tsx should NOT have its own setInterval.
 */
export function usePomodoroTick() {
  const phase = usePomodoroStore((s) => s.phase);
  useEffect(() => {
    if (phase === 'idle') return;
    const id = setInterval(() => usePomodoroStore.getState().tick(), 1000);
    return () => clearInterval(id);
  }, [phase]);
}

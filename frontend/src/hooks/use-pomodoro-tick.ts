import { useEffect } from 'react';
import { usePomodoroStore } from '@/stores/pomodoro-store';

/**
 * Global tick driver for the Pomodoro timer.
 * Mount once (in AppShell) so the interval survives navigation.
 * This is the single source of the tick interval — no component
 * should run its own setInterval against the store.
 */
export function usePomodoroTick() {
  const phase = usePomodoroStore((s) => s.phase);
  useEffect(() => {
    if (phase === 'idle') return;
    const id = setInterval(() => usePomodoroStore.getState().tick(), 1000);
    return () => clearInterval(id);
  }, [phase]);
}

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { usePomodoroStore } from '@/stores/pomodoro-store';

// ---------------------------------------------------------------------------
// POMO-01 regression: v0 blobs with running-timer state must NOT resurrect
// ---------------------------------------------------------------------------
describe('PomodoroStore persist migration — v0 blob never rehydrates running timer', () => {
  it('a stale v0 blob with phase:work rehydrates as idle (migration strips timer state)', async () => {
    // Seed localStorage with a v0-style blob (no `version` key = version 0).
    // This matches the exact shape the operator had persisted (the reported bug).
    const oldTimestamp = Date.now() - 3600_000; // 1 hour ago
    const staleBlob = {
      state: {
        targetCycles: 4,
        workMinutes: 25,
        shortBreakMinutes: 5,
        longBreakMinutes: 15,
        // Running-timer state that MUST be stripped by the v1 migration:
        phase: 'work',
        startedAt: oldTimestamp,
        pausedAt: null,
        totalPausedMs: 0,
        phaseDurationMs: 25 * 60 * 1000,
        cyclesCompleted: 1,
        attachedItem: { id: 99, title: 'Old task', type: 'task' },
        lastWorkElapsedMs: 0,
      },
      // A real pre-v1 blob: zustand persisted it with version 0 (it always
      // writes a version), so the v1 `migrate` fires and strips the timer state.
      version: 0,
    };
    localStorage.setItem('jarvis-pomodoro', JSON.stringify(staleBlob));

    // Force a fresh module load so the store rehydrates from the seeded blob.
    vi.resetModules();
    const { usePomodoroStore: freshStore } = await import('@/stores/pomodoro-store');

    // Wait for the async rehydration (persist middleware schedules it as a microtask)
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    for (let i = 0; i < 10; i++) await Promise.resolve();

    const s = freshStore.getState();
    // POMO-01: phase must be idle (no resurrected countdown)
    expect(s.phase).toBe('idle');
    expect(s.startedAt).toBeNull();
    expect(s.secondsRemaining).toBe(0);
    // Settings must survive the migration
    expect(s.targetCycles).toBe(4);
    expect(s.workMinutes).toBe(25);
  });
});

describe('PomodoroStore', () => {
  beforeEach(() => {
    usePomodoroStore.getState().reset();
    vi.restoreAllMocks();
  });

  it('initializes in idle with null timestamps', () => {
    const s = usePomodoroStore.getState();
    expect(s.phase).toBe('idle');
    expect(s.startedAt).toBeNull();
    expect(s.pausedAt).toBeNull();
    expect(s.totalPausedMs).toBe(0);
    expect(s.secondsRemaining).toBe(0);
    expect(s.cyclesCompleted).toBe(0);
    expect(s.completedSession).toBeNull();
  });

  it('startWork sets phase to work with wall-clock start', () => {
    const now = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(now);
    usePomodoroStore.getState().startWork();
    const s = usePomodoroStore.getState();
    expect(s.phase).toBe('work');
    expect(s.startedAt).toBe(now);
    expect(s.pausedAt).toBeNull();
    expect(s.secondsRemaining).toBe(25 * 60);
  });

  it('startWork attaches an item', () => {
    usePomodoroStore.getState().startWork({ id: 1, title: 'Test', type: 'task' });
    expect(usePomodoroStore.getState().attachedItem).toEqual({ id: 1, title: 'Test', type: 'task' });
  });

  it('tick computes remaining from wall clock', () => {
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork();
    // Advance 10 seconds
    vi.spyOn(Date, 'now').mockReturnValue(start + 10000);
    usePomodoroStore.getState().tick();
    expect(usePomodoroStore.getState().secondsRemaining).toBe(25 * 60 - 10);
  });

  it('tick at work completion creates completedSession and transitions to short break', () => {
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork({ id: 5, title: 'Paper', type: 'paper' });
    // Advance past 25 minutes
    vi.spyOn(Date, 'now').mockReturnValue(start + 25 * 60 * 1000 + 1);
    usePomodoroStore.getState().tick();
    const s = usePomodoroStore.getState();
    expect(s.phase).toBe('short-break');
    expect(s.cyclesCompleted).toBe(1);
    expect(s.completedSession).toEqual({
      durationSeconds: 25 * 60,
      paperId: 5,
    });
  });

  it('tick at short break completion auto-starts next work', () => {
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork();
    // Complete work -> short break
    vi.spyOn(Date, 'now').mockReturnValue(start + 25 * 60 * 1000 + 1);
    usePomodoroStore.getState().tick();
    usePomodoroStore.getState().clearCompletedSession();
    // Complete short break -> should auto-start work, NOT go idle
    const breakStart = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(breakStart + 5 * 60 * 1000 + 1);
    usePomodoroStore.getState().tick();
    const s = usePomodoroStore.getState();
    expect(s.phase).toBe('work');
    expect(s.cyclesCompleted).toBe(1); // preserved from before
  });

  it('tick transitions to long break after target cycles', () => {
    usePomodoroStore.setState({ cyclesCompleted: 3 });
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork();
    // Complete work
    vi.spyOn(Date, 'now').mockReturnValue(start + 25 * 60 * 1000 + 1);
    usePomodoroStore.getState().tick();
    const s = usePomodoroStore.getState();
    expect(s.phase).toBe('long-break');
    expect(s.cyclesCompleted).toBe(4);
  });

  it('tick at long break completion resets to idle', () => {
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.setState({
      phase: 'long-break',
      startedAt: start,
      pausedAt: null,
      totalPausedMs: 0,
      secondsRemaining: 15 * 60,
      cyclesCompleted: 4,
    });
    // Complete long break
    vi.spyOn(Date, 'now').mockReturnValue(start + 15 * 60 * 1000 + 1);
    usePomodoroStore.getState().tick();
    const s = usePomodoroStore.getState();
    expect(s.phase).toBe('idle');
    expect(s.cyclesCompleted).toBe(0);
    expect(s.attachedItem).toBeNull();
  });

  it('pause sets pausedAt and tick does not decrement', () => {
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork();
    // Advance 5 sec, then pause
    vi.spyOn(Date, 'now').mockReturnValue(start + 5000);
    usePomodoroStore.getState().tick();
    const before = usePomodoroStore.getState().secondsRemaining;
    usePomodoroStore.getState().pause();
    expect(usePomodoroStore.getState().pausedAt).not.toBeNull();
    // Advance 10 more sec while paused
    vi.spyOn(Date, 'now').mockReturnValue(start + 15000);
    usePomodoroStore.getState().tick();
    // Should NOT have changed (tick returns early when paused)
    expect(usePomodoroStore.getState().secondsRemaining).toBe(before);
  });

  it('resume accumulates pause time and timer continues correctly', () => {
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork();
    // Work 5 sec
    vi.spyOn(Date, 'now').mockReturnValue(start + 5000);
    usePomodoroStore.getState().pause();
    // Paused for 10 sec
    vi.spyOn(Date, 'now').mockReturnValue(start + 15000);
    usePomodoroStore.getState().resume();
    expect(usePomodoroStore.getState().totalPausedMs).toBe(10000);
    // Tick — elapsed should be 15000 - 10000 = 5000ms = 5 sec of work
    usePomodoroStore.getState().tick();
    expect(usePomodoroStore.getState().secondsRemaining).toBe(25 * 60 - 5);
  });

  it('stopAndLog during work returns elapsed seconds', () => {
    const start = 1000000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork({ id: 3, title: 'Task', type: 'task' });
    // Work 10 minutes
    vi.spyOn(Date, 'now').mockReturnValue(start + 10 * 60 * 1000);
    const result = usePomodoroStore.getState().stopAndLog();
    expect(result).not.toBeNull();
    expect(result!.durationSeconds).toBeCloseTo(600, 0); // 10 min
    expect(result!.taskId).toBe(3);
    expect(usePomodoroStore.getState().phase).toBe('idle');
  });

  it('stopAndLog while idle returns null', () => {
    expect(usePomodoroStore.getState().stopAndLog()).toBeNull();
  });

  it('clearCompletedSession nulls the signal', () => {
    usePomodoroStore.setState({ completedSession: { durationSeconds: 100 } });
    usePomodoroStore.getState().clearCompletedSession();
    expect(usePomodoroStore.getState().completedSession).toBeNull();
  });

  it('restores a Telegram-started server session after a browser reload', () => {
    usePomodoroStore.getState().applyServerSession({
      id: 41,
      state: 'active',
      source: 'telegram',
      duration_seconds: 1500,
      remaining_seconds: 1200,
      started_at: '2026-08-09T12:00:00+00:00',
      paused_at: null,
      paused_seconds: 0,
      completed_at: null,
      recorded_seconds: 0,
      task_id: null,
      paper_id: null,
    });

    const state = usePomodoroStore.getState();
    expect(state.phase).toBe('work');
    expect(state.sessionId).toBe(41);
    expect(state.serverSource).toBe('telegram');
    expect(state.secondsRemaining).toBe(1200);
  });

  it('derives pause and resume operations from the same server session', () => {
    usePomodoroStore.getState().applyServerSession({
      id: 42,
      state: 'paused',
      source: 'web',
      duration_seconds: 1500,
      remaining_seconds: 900,
      started_at: '2026-08-09T12:00:00+00:00',
      paused_at: '2026-08-09T12:10:00+00:00',
      paused_seconds: 30,
      completed_at: null,
      recorded_seconds: 0,
      task_id: 7,
      paper_id: null,
    });

    expect(usePomodoroStore.getState().pausedAt).not.toBeNull();
    usePomodoroStore.getState().resume();
    expect(usePomodoroStore.getState().pendingOperation).toMatchObject({
      kind: 'resume',
      sessionId: 42,
    });
  });

  it('records one local completion signal only after the server completes work', () => {
    usePomodoroStore.setState({
      phase: 'work',
      sessionId: 43,
      serverSource: 'telegram',
      startedAt: Date.now() - 60_000,
      phaseDurationMs: 60_000,
    });
    usePomodoroStore.getState().applyServerSession({
      id: 43,
      state: 'completed',
      source: 'telegram',
      duration_seconds: 60,
      remaining_seconds: 0,
      started_at: '2026-08-09T12:00:00+00:00',
      paused_at: null,
      paused_seconds: 0,
      completed_at: '2026-08-09T12:01:00+00:00',
      recorded_seconds: 60,
      task_id: null,
      paper_id: null,
    });

    const state = usePomodoroStore.getState();
    expect(state.phase).toBe('short-break');
    expect(state.sessionId).toBeNull();
    expect(state.completedSession).toEqual({ durationSeconds: 60 });
  });
});

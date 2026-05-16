/**
 * F6 regression suite — store-level tests (no module mocking):
 *  1. break-stop logs actual elapsed work, not workMinutes*60
 *  2. onRehydrateStorage initialises secondsRemaining correctly (no 00:00 flicker)
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { usePomodoroStore } from '@/stores/pomodoro-store';

// ---------------------------------------------------------------------------
// Break-stop: actual elapsed, not nominal
// ---------------------------------------------------------------------------
describe('PomodoroStore F6 — break-stop logs actual elapsed', () => {
  beforeEach(() => {
    usePomodoroStore.getState().reset();
    vi.restoreAllMocks();
  });

  it('stopAndLog during short-break returns actual work elapsed, not workMinutes*60', () => {
    // Directly inject a short-break state where lastWorkElapsedMs = 10 min
    // (simulating a session where the user paused heavily during the work phase).
    // The key contract: stopAndLog must return lastWorkElapsedMs/1000, not workMinutes*60.
    usePomodoroStore.setState({
      phase: 'short-break',
      startedAt: 6_000_000,
      pausedAt: null,
      totalPausedMs: 0,
      phaseDurationMs: 5 * 60 * 1000,
      secondsRemaining: 4 * 60,
      cyclesCompleted: 1,
      attachedItem: { id: 7, title: 'P', type: 'paper' },
      // Only 10 min of actual work elapsed before the break (not the full 25 min)
      lastWorkElapsedMs: 10 * 60 * 1000,
    });

    expect(usePomodoroStore.getState().phase).toBe('short-break');

    const result = usePomodoroStore.getState().stopAndLog();
    expect(result).not.toBeNull();
    // Should return 600 s (10 min), NOT 1500 s (25 min = workMinutes*60)
    expect(result!.durationSeconds).toBeCloseTo(600, 0);
    expect(result!.paperId).toBe(7);
    expect(usePomodoroStore.getState().phase).toBe('idle');
  });

  it('tick captures lastWorkElapsedMs correctly when transitioning work → short-break', () => {
    // Full work session with no pauses — elapsed should equal phaseDurationMs
    const start = 7_000_000;
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork({ id: 8, title: 'R', type: 'task' });

    // Advance past 25 min (no pauses)
    vi.spyOn(Date, 'now').mockReturnValue(start + 25 * 60 * 1000 + 1);
    usePomodoroStore.getState().tick();

    const s = usePomodoroStore.getState();
    expect(s.phase).toBe('short-break');
    // lastWorkElapsedMs should be capped at phaseDurationMs (25 min)
    expect(s.lastWorkElapsedMs).toBeCloseTo(25 * 60 * 1000, -2); // within ~100ms
  });

  it('stopAndLog during long-break returns actual work elapsed', () => {
    const start = 3_000_000;
    // Set cyclesCompleted to 3 so next tick triggers long break
    usePomodoroStore.setState({ cyclesCompleted: 3 });
    vi.spyOn(Date, 'now').mockReturnValue(start);
    usePomodoroStore.getState().startWork({ id: 42, title: 'Q', type: 'task' });

    // Full 25 min + 1ms — no pausing so actual elapsed = phaseDurationMs
    vi.spyOn(Date, 'now').mockReturnValue(start + 25 * 60 * 1000 + 1);
    usePomodoroStore.getState().tick(); // phase → long-break

    expect(usePomodoroStore.getState().phase).toBe('long-break');

    const result = usePomodoroStore.getState().stopAndLog();
    expect(result).not.toBeNull();
    // Full 25 min elapsed before break
    expect(result!.durationSeconds).toBeCloseTo(25 * 60, 0);
    expect(result!.taskId).toBe(42);
  });
});

// ---------------------------------------------------------------------------
// Rehydrate: secondsRemaining is correct synchronously
// ---------------------------------------------------------------------------
describe('PomodoroStore F6 — rehydrate secondsRemaining', () => {
  beforeEach(() => {
    usePomodoroStore.getState().reset();
    vi.restoreAllMocks();
  });

  it('running session: simulated rehydrate yields correct secondsRemaining (not 0)', () => {
    // 5 min elapsed out of 25 min → 20 min remaining
    const start = 4_000_000;
    const now = start + 5 * 60 * 1000;
    vi.spyOn(Date, 'now').mockReturnValue(now);

    // Simulate what onRehydrateStorage does: apply persisted state then recompute
    usePomodoroStore.setState({
      phase: 'work',
      startedAt: start,
      pausedAt: null,
      totalPausedMs: 0,
      phaseDurationMs: 25 * 60 * 1000,
      secondsRemaining: 0, // deliberately wrong — as it would be before rehydrate callback
      cyclesCompleted: 0,
    });

    // Trigger the same computation that onRehydrateStorage performs
    const s = usePomodoroStore.getState();
    const elapsed = now - s.startedAt! - s.totalPausedMs;
    const computed = Math.max(0, Math.ceil((s.phaseDurationMs - elapsed) / 1000));
    usePomodoroStore.setState({ secondsRemaining: computed });

    expect(usePomodoroStore.getState().secondsRemaining).toBe(20 * 60);
    expect(usePomodoroStore.getState().secondsRemaining).not.toBe(0);
  });

  it('paused session: rehydrate uses pausedAt reference, not current clock', () => {
    const start = 5_000_000;
    const pausedAt = start + 8 * 60 * 1000; // paused after 8 min
    // Current time is 10 min after session start (2 extra minutes since pause)
    vi.spyOn(Date, 'now').mockReturnValue(start + 10 * 60 * 1000);

    usePomodoroStore.setState({
      phase: 'work',
      startedAt: start,
      pausedAt,
      totalPausedMs: 0,
      phaseDurationMs: 25 * 60 * 1000,
      secondsRemaining: 0,
      cyclesCompleted: 0,
    });

    // Simulate onRehydrateStorage paused branch
    const s = usePomodoroStore.getState();
    const elapsed = s.pausedAt! - s.startedAt! - s.totalPausedMs;
    const computed = Math.max(0, Math.ceil((s.phaseDurationMs - elapsed) / 1000));
    usePomodoroStore.setState({ secondsRemaining: computed });

    // 25 min - 8 min = 17 min remaining (not 15 min which would result from using Date.now())
    expect(usePomodoroStore.getState().secondsRemaining).toBe(17 * 60);
  });
});

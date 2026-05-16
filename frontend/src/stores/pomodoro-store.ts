import { create } from 'zustand';
import { persist } from 'zustand/middleware';

type TimerPhase = 'idle' | 'work' | 'short-break' | 'long-break';

interface AttachedItem {
  id: number;
  title: string;
  type: 'task' | 'paper';
}

interface CompletedSession {
  durationSeconds: number;
  taskId?: number;
  paperId?: number;
}

interface PomodoroState {
  // Timer state (persisted — survives refresh)
  phase: TimerPhase;
  startedAt: number | null;        // Date.now() when current phase began
  pausedAt: number | null;         // Date.now() when paused (null = running)
  totalPausedMs: number;           // accumulated pause time in current phase
  phaseDurationMs: number;         // frozen duration for current phase (avoids mid-session setting changes)
  cyclesCompleted: number;
  attachedItem: AttachedItem | null;
  /**
   * How many milliseconds of actual work were elapsed when the most-recent
   * work phase ended (either by natural completion or manual stop during a
   * break). Persisted so stop-during-break after a refresh logs the right
   * amount rather than the full nominal workMinutes.
   */
  lastWorkElapsedMs: number;

  // Computed (NOT persisted — recomputed each tick)
  secondsRemaining: number;

  // Ephemeral signal (NOT persisted)
  completedSession: CompletedSession | null;

  // Settings (persisted)
  targetCycles: number;
  workMinutes: number;
  shortBreakMinutes: number;
  longBreakMinutes: number;

  // Actions
  startWork: (item?: AttachedItem) => void;
  tick: () => void;
  pause: () => void;
  resume: () => void;
  skipBreak: () => void;
  stopAndLog: () => { durationSeconds: number; taskId?: number; paperId?: number } | null;
  clearCompletedSession: () => void;
  reset: () => void;
  /** Alias for reset — called on logout to prevent cross-user leakage. */
  _reset: () => void;
}

export const usePomodoroStore = create<PomodoroState>()(
  persist(
    (set, get) => ({
      // Timer state defaults
      phase: 'idle' as TimerPhase,
      startedAt: null,
      pausedAt: null,
      totalPausedMs: 0,
      phaseDurationMs: 0,
      secondsRemaining: 0,
      cyclesCompleted: 0,
      attachedItem: null,
      lastWorkElapsedMs: 0,

      // Ephemeral signal
      completedSession: null,

      // Settings defaults
      targetCycles: 4,
      workMinutes: 25,
      shortBreakMinutes: 5,
      longBreakMinutes: 15,

      startWork(item?: AttachedItem) {
        const state = get();
        const durationMs = state.workMinutes * 60 * 1000;
        set({
          phase: 'work',
          startedAt: Date.now(),
          pausedAt: null,
          totalPausedMs: 0,
          phaseDurationMs: durationMs,
          secondsRemaining: state.workMinutes * 60,
          attachedItem: item ?? null,
          lastWorkElapsedMs: 0,
        });
      },

      tick() {
        const state = get();

        // No-op when idle, paused, or missing start time
        if (state.phase === 'idle' || state.pausedAt !== null || state.startedAt === null) {
          return;
        }

        const now = Date.now();
        const elapsed = now - state.startedAt - state.totalPausedMs;

        const remainingSec = Math.max(0, Math.ceil((state.phaseDurationMs - elapsed) / 1000));

        if (remainingSec <= 0) {
          // Phase expired — transition
          if (state.phase === 'work') {
            const newCycles = state.cyclesCompleted + 1;
            // Actual elapsed work time — cap at phaseDurationMs (can't exceed it)
            const workElapsedMs = Math.min(state.phaseDurationMs, elapsed);

            // Build completedSession signal for auto-logging
            const session: CompletedSession = {
              durationSeconds: state.phaseDurationMs / 1000,
            };
            if (state.attachedItem) {
              if (state.attachedItem.type === 'task') {
                session.taskId = state.attachedItem.id;
              } else {
                session.paperId = state.attachedItem.id;
              }
            }

            if (newCycles >= state.targetCycles) {
              // Long break after completing all target cycles
              set({
                phase: 'long-break',
                startedAt: Date.now(),
                pausedAt: null,
                totalPausedMs: 0,
                phaseDurationMs: state.longBreakMinutes * 60 * 1000,
                secondsRemaining: state.longBreakMinutes * 60,
                cyclesCompleted: newCycles,
                completedSession: session,
                lastWorkElapsedMs: workElapsedMs,
              });
            } else {
              // Short break
              set({
                phase: 'short-break',
                startedAt: Date.now(),
                pausedAt: null,
                totalPausedMs: 0,
                phaseDurationMs: state.shortBreakMinutes * 60 * 1000,
                secondsRemaining: state.shortBreakMinutes * 60,
                cyclesCompleted: newCycles,
                completedSession: session,
                lastWorkElapsedMs: workElapsedMs,
              });
            }
          } else if (state.phase === 'short-break') {
            // Bug #4 fix: auto-start next work session instead of going idle
            set({
              phase: 'work',
              startedAt: Date.now(),
              pausedAt: null,
              totalPausedMs: 0,
              phaseDurationMs: state.workMinutes * 60 * 1000,
              secondsRemaining: state.workMinutes * 60,
              lastWorkElapsedMs: 0,
            });
          } else {
            // Long break ended — full reset
            set({
              phase: 'idle',
              startedAt: null,
              pausedAt: null,
              totalPausedMs: 0,
              phaseDurationMs: 0,
              secondsRemaining: 0,
              cyclesCompleted: 0,
              attachedItem: null,
            });
          }
        } else {
          set({ secondsRemaining: remainingSec });
        }
      },

      pause() {
        const state = get();
        // Only allow pausing during work phase, and only if not already paused
        if (state.phase === 'work' && state.pausedAt === null && state.startedAt !== null) {
          set({ pausedAt: Date.now() });
        }
      },

      resume() {
        const state = get();
        if (state.pausedAt !== null) {
          const additionalPause = Date.now() - state.pausedAt;
          set({
            pausedAt: null,
            totalPausedMs: state.totalPausedMs + additionalPause,
          });
        }
      },

      skipBreak() {
        const state = get();
        if (state.phase === 'short-break' || state.phase === 'long-break') {
          set({
            phase: 'work',
            startedAt: Date.now(),
            pausedAt: null,
            totalPausedMs: 0,
            phaseDurationMs: state.workMinutes * 60 * 1000,
            secondsRemaining: state.workMinutes * 60,
          });
        }
      },

      stopAndLog() {
        const state = get();
        if (state.phase === 'idle' || state.startedAt === null) return null;

        let durationSeconds: number;

        if (state.phase === 'work') {
          // Currently in work phase — calculate wall-clock elapsed time
          const pauseAdjustment = state.pausedAt !== null
            ? Date.now() - state.pausedAt
            : 0;
          const elapsed = Date.now() - state.startedAt - state.totalPausedMs - pauseAdjustment;
          durationSeconds = Math.max(0, elapsed / 1000);
        } else {
          // In a break — log the actual elapsed work from the prior work phase,
          // not the full nominal duration (which over-reports if work was cut short).
          durationSeconds = state.lastWorkElapsedMs / 1000;
        }

        const result: { durationSeconds: number; taskId?: number; paperId?: number } = {
          durationSeconds,
        };

        if (state.attachedItem) {
          if (state.attachedItem.type === 'task') {
            result.taskId = state.attachedItem.id;
          } else {
            result.paperId = state.attachedItem.id;
          }
        }

        // Full reset
        set({
          phase: 'idle',
          startedAt: null,
          pausedAt: null,
          totalPausedMs: 0,
          phaseDurationMs: 0,
          secondsRemaining: 0,
          cyclesCompleted: 0,
          attachedItem: null,
        });

        return result;
      },

      clearCompletedSession() {
        set({ completedSession: null });
      },

      reset() {
        set({
          phase: 'idle',
          startedAt: null,
          pausedAt: null,
          totalPausedMs: 0,
          phaseDurationMs: 0,
          secondsRemaining: 0,
          cyclesCompleted: 0,
          attachedItem: null,
          completedSession: null,
          lastWorkElapsedMs: 0,
        });
      },

      _reset() {
        get().reset();
      },
    }),
    {
      name: 'jarvis-pomodoro',
      partialize: (state) => ({
        // Settings
        targetCycles: state.targetCycles,
        workMinutes: state.workMinutes,
        shortBreakMinutes: state.shortBreakMinutes,
        longBreakMinutes: state.longBreakMinutes,
        // Timer state (survives refresh)
        phase: state.phase,
        phaseDurationMs: state.phaseDurationMs,
        startedAt: state.startedAt,
        pausedAt: state.pausedAt,
        totalPausedMs: state.totalPausedMs,
        cyclesCompleted: state.cyclesCompleted,
        attachedItem: state.attachedItem,
        lastWorkElapsedMs: state.lastWorkElapsedMs,
        // NOT persisted: secondsRemaining (recomputed), completedSession (ephemeral)
      }),
      onRehydrateStorage: () => (state) => {
        // Recompute secondsRemaining synchronously from persisted timestamps so
        // the header never shows 00:00 for a live session on first paint.
        if (!state || state.phase === 'idle' || !state.startedAt) return;
        const now = Date.now();
        if (state.pausedAt !== null) {
          // Still paused — use pausedAt as the reference point
          const elapsed = state.pausedAt - state.startedAt - state.totalPausedMs;
          state.secondsRemaining = Math.max(0, Math.ceil((state.phaseDurationMs - elapsed) / 1000));
        } else {
          // Running — compute live remaining
          const elapsed = now - state.startedAt - state.totalPausedMs;
          state.secondsRemaining = Math.max(0, Math.ceil((state.phaseDurationMs - elapsed) / 1000));
        }
      },
    },
  ),
);

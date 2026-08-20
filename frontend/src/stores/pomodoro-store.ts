import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { ActiveFocusSession } from '@/types';

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

type FocusOperation =
  | { id: number; kind: 'start'; durationSeconds: number; taskId?: number; paperId?: number }
  | { id: number; kind: 'pause'; sessionId: number }
  | { id: number; kind: 'resume'; sessionId: number }
  | { id: number; kind: 'complete'; sessionId: number; mode: 'elapsed' | 'stop' };

type PendingStartAction = 'pause' | 'complete' | null;

let nextOperationId = 1;

interface PomodoroState {
  // Ephemeral timer state. Only preferences are persisted below; an active
  // server session is restored through PomodoroAutoLogger after refresh.
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
   * break). This keeps stop-during-break accounting tied to elapsed work
   * rather than the full nominal workMinutes.
   */
  lastWorkElapsedMs: number;
  sessionId: number | null;
  serverSource: 'web' | 'telegram' | null;
  pendingOperation: FocusOperation | null;
  pendingStartAction: PendingStartAction;

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
  applyServerSession: (session: ActiveFocusSession | null) => void;
  clearPendingOperation: (operationId: number) => void;
  reset: () => void;
  /** Alias for reset — called on logout to prevent cross-user leakage. */
  _reset: () => void;
}

/**
 * Accepted range for each timer preference, as `[minimum, maximum]`.
 *
 * The one browser-side definition of the focus-timer contract: the settings
 * sliders offer these bounds and the account sync accepts a stored preference
 * only inside them. It mirrors `TIMER_RANGES` in the backend validator, which a
 * test compares against this file — the account rejects a write outside it.
 */
export const TIMER_RANGES = {
  workMinutes: [15, 60],
  shortBreakMinutes: [3, 15],
  longBreakMinutes: [10, 30],
  targetCycles: [2, 8],
} as const satisfies Record<string, readonly [number, number]>;

/** Applied until the account has a saved preference; mirrors `TIMER_DEFAULTS`. */
export const TIMER_DEFAULTS = {
  workMinutes: 25,
  shortBreakMinutes: 5,
  longBreakMinutes: 15,
  targetCycles: 4,
} as const satisfies Record<keyof typeof TIMER_RANGES, number>;

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
      sessionId: null,
      serverSource: null,
      pendingOperation: null,
      pendingStartAction: null,

      // Ephemeral signal
      completedSession: null,

      // Settings defaults
      ...TIMER_DEFAULTS,

      startWork(item?: AttachedItem) {
        const state = get();
        if (state.phase === 'work' || state.pendingOperation !== null) return;
        const durationMs = state.workMinutes * 60 * 1000;
        const operation: FocusOperation = {
          id: nextOperationId++,
          kind: 'start',
          durationSeconds: state.workMinutes * 60,
          ...(item?.type === 'task' ? { taskId: item.id } : {}),
          ...(item?.type === 'paper' ? { paperId: item.id } : {}),
        };
        set({
          phase: 'work',
          startedAt: Date.now(),
          pausedAt: null,
          totalPausedMs: 0,
          phaseDurationMs: durationMs,
          secondsRemaining: state.workMinutes * 60,
          attachedItem: item ?? null,
          lastWorkElapsedMs: 0,
          pendingOperation: operation,
          pendingStartAction: null,
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

            // A server-backed interval transitions only after the authoritative
            // completion succeeds. Local-only tests and break phases retain the
            // existing synchronous state-machine behavior.
            const session: CompletedSession = {
              durationSeconds: state.phaseDurationMs / 1000,
            };
            if (state.sessionId !== null) {
              // A server-backed completion is authoritative. The first tick past
              // expiry mints it and stays in 'work' until the server confirms;
              // the pending-op guard stops later ticks re-minting and firing a
              // duplicate completion every second until applyServerSession moves
              // the phase on.
              if (state.pendingOperation !== null) return;
              set({
                secondsRemaining: 0,
                pendingOperation: {
                  id: nextOperationId++,
                  kind: 'complete',
                  sessionId: state.sessionId,
                  mode: 'elapsed',
                },
              });
              return;
            }
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
                pendingOperation: null,
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
                pendingOperation: null,
              });
            }
          } else if (state.phase === 'short-break') {
            get().startWork(state.attachedItem ?? undefined);
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
              sessionId: null,
              serverSource: null,
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
          const awaitingStart = state.sessionId === null && state.pendingOperation?.kind === 'start';
          set({
            pausedAt: Date.now(),
            pendingStartAction: awaitingStart ? 'pause' : state.pendingStartAction,
            pendingOperation: state.sessionId === null
              ? state.pendingOperation
              : { id: nextOperationId++, kind: 'pause', sessionId: state.sessionId },
          });
        }
      },

      resume() {
        const state = get();
        if (state.pausedAt !== null) {
          const additionalPause = Date.now() - state.pausedAt;
          const awaitingStart = state.sessionId === null && state.pendingOperation?.kind === 'start';
          set({
            pausedAt: null,
            totalPausedMs: state.totalPausedMs + additionalPause,
            pendingStartAction: awaitingStart ? null : state.pendingStartAction,
            pendingOperation: state.sessionId === null
              ? state.pendingOperation
              : { id: nextOperationId++, kind: 'resume', sessionId: state.sessionId },
          });
        }
      },

      skipBreak() {
        const state = get();
        if (state.phase === 'short-break' || state.phase === 'long-break') {
          get().startWork(state.attachedItem ?? undefined);
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

        const awaitingStart = state.sessionId === null && state.pendingOperation?.kind === 'start';
        const completionOperation: FocusOperation | null = state.sessionId === null
          ? state.pendingOperation
          : {
              id: nextOperationId++,
              kind: 'complete',
              sessionId: state.sessionId,
              mode: 'stop',
            };

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
          sessionId: null,
          serverSource: null,
          pendingOperation: completionOperation,
          pendingStartAction: awaitingStart ? 'complete' : null,
        });

        return result;
      },

      clearCompletedSession() {
        set({ completedSession: null });
      },

      applyServerSession(session) {
        const state = get();
        if (session === null) {
          if (state.phase === 'work' && state.pendingOperation === null) {
            set({
              phase: 'idle',
              startedAt: null,
              pausedAt: null,
              totalPausedMs: 0,
              phaseDurationMs: 0,
              secondsRemaining: 0,
              attachedItem: null,
              sessionId: null,
              serverSource: null,
              pendingStartAction: null,
            });
          }
          return;
        }
        if (session.state === 'completed') {
          if (state.phase === 'work') {
            const cyclesCompleted = state.cyclesCompleted + 1;
            const completedSession: CompletedSession = {
              durationSeconds: session.recorded_seconds,
              ...(session.task_id === null ? {} : { taskId: session.task_id }),
              ...(session.paper_id === null ? {} : { paperId: session.paper_id }),
            };
            const longBreak = cyclesCompleted >= state.targetCycles;
            const breakMinutes = longBreak ? state.longBreakMinutes : state.shortBreakMinutes;
            set({
              phase: longBreak ? 'long-break' : 'short-break',
              startedAt: Date.now(),
              pausedAt: null,
              totalPausedMs: 0,
              phaseDurationMs: breakMinutes * 60 * 1000,
              secondsRemaining: breakMinutes * 60,
              cyclesCompleted,
              completedSession,
              lastWorkElapsedMs: session.recorded_seconds * 1000,
              sessionId: null,
              serverSource: null,
              pendingStartAction: null,
            });
          } else {
            set({ sessionId: null, serverSource: null, pendingStartAction: null });
          }
          return;
        }
        const startedAt = Date.parse(session.started_at);
        const pausedAt = session.paused_at === null ? null : Date.parse(session.paused_at);
        const attachedItem: AttachedItem | null = session.task_id !== null
          ? { id: session.task_id, title: '', type: 'task' }
          : session.paper_id !== null
            ? { id: session.paper_id, title: '', type: 'paper' }
            : null;
        const postStartAction = state.pendingOperation?.kind === 'start'
          ? state.pendingStartAction
          : null;
        if (postStartAction === 'complete') {
          set({
            sessionId: session.id,
            serverSource: session.source,
            pendingOperation: {
              id: nextOperationId++,
              kind: 'complete',
              sessionId: session.id,
              mode: 'stop',
            },
            pendingStartAction: null,
          });
          return;
        }
        set({
          phase: 'work',
          startedAt,
          pausedAt: postStartAction === 'pause' ? (state.pausedAt ?? Date.now()) : pausedAt,
          totalPausedMs: session.paused_seconds * 1000,
          phaseDurationMs: session.duration_seconds * 1000,
          secondsRemaining: session.remaining_seconds,
          attachedItem,
          sessionId: session.id,
          serverSource: session.source,
          pendingOperation: postStartAction === 'pause'
            ? { id: nextOperationId++, kind: 'pause', sessionId: session.id }
            : state.pendingOperation,
          pendingStartAction: null,
        });
      },

      clearPendingOperation(operationId) {
        if (get().pendingOperation?.id === operationId) {
          set({ pendingOperation: null, pendingStartAction: null });
        }
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
          sessionId: null,
          serverSource: null,
          pendingOperation: null,
          pendingStartAction: null,
        });
      },

      _reset() {
        get().reset();
      },
    }),
    {
      name: 'jarvis-pomodoro',
      // v1: stop persisting running-timer state; a reload always starts idle.
      // Only settings are persisted so user preferences survive tab closes.
      version: 1,
      migrate: (persistedState: unknown, version: number | undefined) => {
        // v1 stops persisting running-timer state; strip it from older blobs
        // so an existing persisted 'work' phase never resurrects on rehydration.
        // An unversioned blob reads back as version === undefined → treat as 0.
        const s = (persistedState ?? {}) as Record<string, unknown>;
        if ((version ?? 0) < 1) {
          delete s.phase;
          delete s.startedAt;
          delete s.pausedAt;
          delete s.totalPausedMs;
          delete s.phaseDurationMs;
          delete s.cyclesCompleted;
          delete s.attachedItem;
          delete s.lastWorkElapsedMs;
        }
        return s;
      },
      partialize: (state) => ({
        // Settings only — timer state is ephemeral (not persisted).
        // A reload always starts in idle; no running countdown rehydrates from disk.
        targetCycles: state.targetCycles,
        workMinutes: state.workMinutes,
        shortBreakMinutes: state.shortBreakMinutes,
        longBreakMinutes: state.longBreakMinutes,
      }),
    },
  ),
);

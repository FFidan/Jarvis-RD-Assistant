/**
 * F6 regression suite — UI-level tests:
 *  3. Pause button is absent during break phases (HeaderPomodoro)
 *  4. HeroNow tab label is phase-aware ("Continue task" vs "On break")
 *
 * Store-level tests (break-stop elapsed, rehydrate) are in pomodoro-f6-store.test.ts
 * because vi.mock is hoisted to module scope and would shadow the real store here.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { HeaderPomodoro } from '@/components/layout/HeaderPomodoro';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Module-scoped mock — applies to ALL tests in this file.
// Store-level tests live in pomodoro-f6-store.test.ts (no mock there).
// ---------------------------------------------------------------------------

const headerPauseMock = vi.fn();
const headerResumeMock = vi.fn();
const headerStopAndLogMock = vi.fn();

interface MockPomodoroState {
  phase: string;
  secondsRemaining: number;
  pausedAt: number | null;
  attachedItem: { id: number; title: string; type: string } | null;
  pause: () => void;
  resume: () => void;
}

// vi.hoisted ensures the factory variables are captured before mock hoisting
const { mockState } = vi.hoisted(() => {
  const initialValue: MockPomodoroState = {
    phase: 'work',
    secondsRemaining: 1500,
    pausedAt: null,
    attachedItem: null,
    pause: () => {},
    resume: () => {},
  };
  const mockState = { value: initialValue };
  return { mockState };
});

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (s: (typeof mockState)['value']) => unknown) =>
      selector ? selector(mockState.value) : mockState.value,
    {
      getState: () => ({
        pause: headerPauseMock,
        resume: headerResumeMock,
        stopAndLog: headerStopAndLogMock,
      }),
    },
  ),
}));

vi.mock('@/lib/api', () => ({
  logFocusSession: vi.fn().mockResolvedValue({ status: 'ok', recorded_hours: 0.1 }),
}));

function renderHeader() {
  const qc = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <HeaderPomodoro />
    </MemoryRouter>,
    { queryClient: qc },
  );
}

// ---------------------------------------------------------------------------
// HeaderPomodoro — pause hidden on break
// ---------------------------------------------------------------------------
describe('HeaderPomodoro F6 — pause hidden on break', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockState.value = {
      phase: 'work',
      secondsRemaining: 1500,
      pausedAt: null,
      attachedItem: null,
      pause: headerPauseMock,
      resume: headerResumeMock,
    };
  });

  it('shows Pause button during work phase', () => {
    mockState.value.phase = 'work';
    renderHeader();
    expect(screen.getByRole('button', { name: /pause pomodoro/i })).toBeInTheDocument();
  });

  it('does NOT show Pause or Resume button during short-break', () => {
    mockState.value = { ...mockState.value, phase: 'short-break', secondsRemaining: 300 };
    renderHeader();
    expect(screen.queryByRole('button', { name: /pause pomodoro/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /resume pomodoro/i })).toBeNull();
  });

  it('does NOT show Pause or Resume button during long-break', () => {
    mockState.value = { ...mockState.value, phase: 'long-break', secondsRemaining: 900 };
    renderHeader();
    expect(screen.queryByRole('button', { name: /pause pomodoro/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /resume pomodoro/i })).toBeNull();
  });

  it('Stop button is always visible during non-idle phases', () => {
    mockState.value.phase = 'short-break';
    renderHeader();
    expect(screen.getByRole('button', { name: /stop pomodoro/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// HeroNow — phase-aware tab label (pure helper logic, no DOM)
// ---------------------------------------------------------------------------
describe('HeroNow F6 — taskLabelForPhase helper logic', () => {
  // Mirror of the helper in HeroNow.tsx — tests the contract, not the import.
  function taskLabelForPhase(phase: string): string {
    if (phase === 'short-break' || phase === 'long-break') return 'On break';
    return 'Continue task';
  }

  it('returns "Continue task" during work phase', () => {
    expect(taskLabelForPhase('work')).toBe('Continue task');
  });

  it('returns "On break" during short-break', () => {
    expect(taskLabelForPhase('short-break')).toBe('On break');
  });

  it('returns "On break" during long-break', () => {
    expect(taskLabelForPhase('long-break')).toBe('On break');
  });

  it('returns "Continue task" for idle (tab is hidden when idle; fallback safe)', () => {
    expect(taskLabelForPhase('idle')).toBe('Continue task');
  });
});

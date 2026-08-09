import { act, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PomodoroAutoLogger } from '@/components/layout/PomodoroAutoLogger';
import { renderWithProviders } from '@/__tests__/test-utils';
import { usePomodoroStore } from '@/stores/pomodoro-store';

const apiMocks = vi.hoisted(() => ({
  fetchActiveFocusSession: vi.fn(),
  startFocusSession: vi.fn(),
  pauseFocusSession: vi.fn(),
  resumeFocusSession: vi.fn(),
  completeFocusSession: vi.fn(),
}));

vi.mock('@/lib/api', () => apiMocks);

const { fetchActiveFocusSession, startFocusSession } = apiMocks;

const activeSession = {
  id: 51,
  state: 'active' as const,
  source: 'telegram' as const,
  duration_seconds: 1500,
  remaining_seconds: 1200,
  started_at: '2026-08-09T12:00:00+00:00',
  paused_at: null,
  paused_seconds: 0,
  completed_at: null,
  recorded_seconds: 0,
  task_id: null,
  paper_id: null,
};

describe('PomodoroAutoLogger durable focus synchronization', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    usePomodoroStore.getState().reset();
    fetchActiveFocusSession.mockResolvedValue(null);
  });

  it('restores a Telegram session and observes external pause and completion', async () => {
    fetchActiveFocusSession.mockResolvedValueOnce(activeSession);
    renderWithProviders(<PomodoroAutoLogger />);

    await waitFor(() => expect(usePomodoroStore.getState().sessionId).toBe(51));
    expect(usePomodoroStore.getState().serverSource).toBe('telegram');

    fetchActiveFocusSession.mockResolvedValueOnce({
      ...activeSession,
      state: 'paused',
      paused_at: '2026-08-09T12:05:00+00:00',
      remaining_seconds: 900,
    });
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      value: 'visible',
    });
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    await waitFor(() => expect(usePomodoroStore.getState().pausedAt).not.toBeNull());

    fetchActiveFocusSession.mockResolvedValueOnce({
      ...activeSession,
      state: 'completed',
      remaining_seconds: 0,
      completed_at: '2026-08-09T12:25:00+00:00',
      recorded_seconds: 1500,
    });
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    await waitFor(() => expect(usePomodoroStore.getState().phase).toBe('short-break'));
    expect(usePomodoroStore.getState().sessionId).toBeNull();
  });

  it('replaces an optimistic Web start with the server session', async () => {
    startFocusSession.mockResolvedValue({ ...activeSession, id: 52, source: 'web' });
    renderWithProviders(<PomodoroAutoLogger />);

    act(() => usePomodoroStore.getState().startWork());

    await waitFor(() => expect(startFocusSession).toHaveBeenCalledOnce());
    await waitFor(() => expect(usePomodoroStore.getState().pendingOperation).toBeNull());
    expect(usePomodoroStore.getState()).toMatchObject({
      phase: 'work',
      sessionId: 52,
      serverSource: 'web',
    });
  });
});

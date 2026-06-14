/**
 * Tests for auth store persistence: API key uses sessionStorage (not localStorage),
 * and logout clears the jarvis-ui localStorage entry.
 *
 * The Zustand persist middleware writes to the storage engine on every
 * state change. In the jsdom environment, sessionStorage and localStorage
 * are real (in-memory) implementations, so we can assert on them directly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Stub fetch BEFORE importing the store so the login call can be controlled.
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

// Dynamic import ensures the stub is in place when the module initialises.
const { useAuthStore } = await import('@/stores/auth-store');
const { useUIStore } = await import('@/stores/ui-store');
const { usePomodoroStore } = await import('@/stores/pomodoro-store');

describe('auth-store — sessionStorage persistence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset in-memory storage between tests.
    sessionStorage.clear();
    localStorage.clear();
    // Reset Zustand state.
    useAuthStore.setState({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,
    });
  });

  it('session user is written to sessionStorage (not localStorage) after login', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ id: 7, email: 'owner@example.com', role: 'admin' }),
    });
    await useAuthStore.getState().login('test-api-key-32chars-xxxxxxxxxx');

    // sessionStorage must contain the persisted auth entry with the session
    // user (the cookie is the credential — no raw apiKey is persisted).
    const raw = sessionStorage.getItem('jarvis-auth');
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.state.user).toEqual({ id: 7, email: 'owner@example.com', role: 'admin' });
    // apiKey must not be persisted to sessionStorage at all.
    expect('apiKey' in (parsed.state as Record<string, unknown>)).toBe(false);

    // localStorage must NOT contain the auth entry.
    expect(localStorage.getItem('jarvis-auth')).toBeNull();
  });

  it('logout clears the session user from sessionStorage', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ id: 7, email: 'owner@example.com', role: 'admin' }),
    });
    await useAuthStore.getState().login('test-api-key-32chars-xxxxxxxxxx');
    expect(sessionStorage.getItem('jarvis-auth')).not.toBeNull();

    useAuthStore.getState().logout();

    // After logout the persisted state must have user: null.
    const raw = sessionStorage.getItem('jarvis-auth');
    // The persist middleware may keep the key with null values, or remove it.
    if (raw !== null) {
      const parsed = JSON.parse(raw);
      expect(parsed.state.user).toBeNull();
      expect(parsed.state.isAuthenticated).toBe(false);
    }
    // Either way, the store state must be cleared.
    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('logout clears stale ui-store state (no dismissed flags survive)', async () => {
    // Simulate pre-existing UI state from a previous session.
    localStorage.setItem('jarvis-ui', JSON.stringify({ state: { checklistDismissed: true }, version: 0 }));

    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ id: 7, email: 'owner@example.com', role: 'admin' }),
    });
    await useAuthStore.getState().login('test-api-key-32chars-xxxxxxxxxx');

    useAuthStore.getState().logout();
    // Flush logout's async resets (Promise.allSettled re-persists clean ui state).
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    for (let i = 0; i < 20; i++) await Promise.resolve();

    // The stale dismissed flag must not survive — the key is either cleared or
    // holds only clean initial state (the in-memory _reset re-persists defaults).
    const raw = localStorage.getItem('jarvis-ui');
    if (raw !== null) {
      const parsed = JSON.parse(raw) as { state: { checklistDismissed?: boolean } };
      expect(parsed.state.checklistDismissed ?? false).toBe(false);
    }
    expect(useUIStore.getState().checklistDismissed).toBe(false);
  });

  // -------------------------------------------------------------------------
  // partialize must never include apiKey (in-memory only)
  // -------------------------------------------------------------------------
  it('apiKey is absent from the partialized state even when set in-memory', () => {
    // Force an apiKey into in-memory state (simulates a legacy/edge-case path).
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'should-never-be-persisted',
      user: { id: 1, email: 'x@example.com', role: 'user' },
    });

    // Trigger a write to sessionStorage by reading the persisted key.
    // The persist middleware writes on any setState, so the key is already there.
    const raw = sessionStorage.getItem('jarvis-auth');
    expect(raw).not.toBeNull();
    const persisted = JSON.parse(raw!) as { state: Record<string, unknown> };

    // The apiKey field must not appear in the persisted payload.
    expect('apiKey' in persisted.state).toBe(false);
    // Non-sensitive fields are still persisted.
    expect(persisted.state.isAuthenticated).toBe(true);
    expect(persisted.state.user).toEqual({ id: 1, email: 'x@example.com', role: 'user' });
  });

  // -------------------------------------------------------------------------
  // UI-STORE-XUSER-LEAK: logout resets ui-store in-memory (not just on disk)
  // -------------------------------------------------------------------------
  it('logout resets ui-store in-memory (checklistDismissed → false)', async () => {
    // Simulate a dismissed checklist from the previous session.
    useUIStore.setState({ checklistDismissed: true, setupBannerDismissed: true });

    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ id: 7, email: 'owner@example.com', role: 'admin' }),
    });
    await useAuthStore.getState().login('test-api-key-32chars-xxxxxxxxxx');

    useAuthStore.getState().logout();

    // Flush async tasks inside logout() (Promise.allSettled + dynamic imports)
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    for (let i = 0; i < 20; i++) await Promise.resolve();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    // In-memory reset — not just the localStorage key removal.
    expect(useUIStore.getState().checklistDismissed).toBe(false);
    expect(useUIStore.getState().setupBannerDismissed).toBe(false);
  });

  // -------------------------------------------------------------------------
  // POMO-01 / UI-STORE-XUSER-LEAK: loginWithSession resets pomodoro + ui stores
  // -------------------------------------------------------------------------
  it('loginWithSession resets pomodoro to idle and ui flags to initial state', () => {
    // Seed stores with dirty state from a previous session.
    usePomodoroStore.setState({ phase: 'work', startedAt: Date.now(), cyclesCompleted: 2 });
    useUIStore.setState({ checklistDismissed: true, paperDetailNoteDismissed: true });

    useAuthStore.getState().loginWithSession({ id: 42, email: 'new@example.com', role: 'user' });

    // Synchronous reset — no async flush needed (static imports in auth-store).
    expect(usePomodoroStore.getState().phase).toBe('idle');
    expect(usePomodoroStore.getState().startedAt).toBeNull();
    expect(useUIStore.getState().checklistDismissed).toBe(false);
    expect(useUIStore.getState().paperDetailNoteDismissed).toBe(false);
  });
});

/**
 * Tests for WS-FRONTEND-HYGIENE: after logout(), the React-Query cache is
 * cleared and all user-scoped zustand stores are reset to their initial state.
 *
 * This prevents cross-user data leakage when two users share a browser
 * session on the same machine.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

/** Flush all pending microtasks (Promise.resolve chain resolves dynamic imports). */
async function flushPromises(): Promise<void> {
  // A few ticks are enough; dynamic imports resolve in a single microtask.
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
}

// --- Stub globals before any store imports ---

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

// navigator.serviceWorker is undefined in jsdom — provide a stub so the
// postMessage call in logout() is exercisable.
const mockPostMessage = vi.fn();
Object.defineProperty(navigator, 'serviceWorker', {
  value: { controller: { postMessage: mockPostMessage } },
  writable: true,
  configurable: true,
});

// --- Dynamic imports (after stubs are in place) ---

const { useAuthStore, registerQueryClient } = await import('@/stores/auth-store');
const { useChatStore } = await import('@/stores/chat-store');
const { useJobStore } = await import('@/stores/job-store');
const { useBulkSelection } = await import('@/stores/bulk-selection-store');
const { usePomodoroStore } = await import('@/stores/pomodoro-store');
const { useKeyboardShortcuts } = await import('@/stores/keyboard-shortcuts-store');

// --- QueryClient mock ---

const mockQueryClientClear = vi.fn();
const fakeQueryClient = { clear: mockQueryClientClear } as unknown as import('@tanstack/react-query').QueryClient;

// --- Helpers ---

/** Seed stores with non-default data to verify they get reset. */
function seedStores() {
  useChatStore.setState({ chats: { 'chat-1': [{ role: 'user', content: 'hello' }] } });
  useJobStore.setState({
    jobs: {
      'job-1': {
        id: 'job-1',
        kind: 'pulse.generate',
        status: 'running',
        progress: 50,
        progress_message: null,
        payload: null,
        result: null,
        error: null,
        created_at: new Date().toISOString(),
        started_at: null,
        finished_at: null,
      },
    },
  });
  useBulkSelection.setState({ selectedIds: new Set([1, 2, 3]) });
  usePomodoroStore.setState({ phase: 'work', startedAt: Date.now() });
  useKeyboardShortcuts.setState({ isOpen: true });
}

describe('logout-hygiene', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    localStorage.clear();

    // Register the fake QueryClient so logout() can call clear() on it.
    registerQueryClient(fakeQueryClient);

    // Seed stores with non-default data.
    seedStores();

    // Put auth store into a logged-in state (API-key path).
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'test-key-32chars-xxxxxxxxxxxx',
      user: null,
    });

    // logout() fires a best-effort POST — always resolve it.
    mockFetch.mockResolvedValue({ ok: true, status: 200 });
  });

  it('calls queryClient.clear() on logout', async () => {
    useAuthStore.getState().logout();
    // Dynamic imports inside logout() are microtask-scheduled — flush them.
    await flushPromises();
    expect(mockQueryClientClear).toHaveBeenCalledOnce();
  });

  it('clears auth state on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().apiKey).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it('resets chat-store to empty on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(useChatStore.getState().chats).toEqual({});
  });

  it('resets job-store to empty on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(useJobStore.getState().jobs).toEqual({});
  });

  it('resets bulk-selection-store to empty on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(useJobStore.getState().jobs).toEqual({});
    const { selectedIds } = useBulkSelection.getState();
    expect(selectedIds.size).toBe(0);
  });

  it('resets pomodoro-store to idle on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(usePomodoroStore.getState().phase).toBe('idle');
    expect(usePomodoroStore.getState().startedAt).toBeNull();
  });

  it('resets keyboard-shortcuts-store to closed on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(useKeyboardShortcuts.getState().isOpen).toBe(false);
  });

  it('posts JARVIS_LOGOUT message to the service worker on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(mockPostMessage).toHaveBeenCalledWith({ type: 'JARVIS_LOGOUT' });
  });

  it('queryClient.clear is not called when no client is registered', async () => {
    // Override the registered client with null to simulate SSR/test environments.
    registerQueryClient(null as unknown as import('@tanstack/react-query').QueryClient);
    mockQueryClientClear.mockClear();
    useAuthStore.getState().logout();
    await flushPromises();
    expect(mockQueryClientClear).not.toHaveBeenCalled();
  });
});

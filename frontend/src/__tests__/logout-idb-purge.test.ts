/**
 * P1c — logout cache-purge wiring tests.
 *
 * Verifies that auth-store's logout() calls clearPersistedQueryCache() from
 * P1b (query-persister), purging the IndexedDB read cache on logout for
 * cross-user data hygiene. Also verifies that:
 *   - The SW postMessage is still posted (defense-in-depth; now done by
 *     clearPersistedQueryCache() + the existing navigator.serviceWorker call).
 *   - Logout still completes even if clearPersistedQueryCache() rejects.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

/** Flush microtasks so dynamic imports + async void chains resolve. */
async function flushPromises(): Promise<void> {
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
}

// ------ Stubs before any imports ------

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

const mockPostMessage = vi.fn();
Object.defineProperty(navigator, 'serviceWorker', {
  value: { controller: { postMessage: mockPostMessage } },
  writable: true,
  configurable: true,
});

// ------ Mock clearPersistedQueryCache so we can spy on it ------
const mockClearPersistedQueryCache = vi.fn().mockResolvedValue(undefined);
vi.mock('@/lib/query-persister', () => ({
  attachQueryPersister: vi.fn().mockReturnValue(() => {}),
  clearPersistedQueryCache: mockClearPersistedQueryCache,
  GC_TIME: 7 * 24 * 60 * 60 * 1000,
  shouldDehydrateQuery: vi.fn().mockReturnValue(false),
  getPersistedCacheTimestamp: vi.fn().mockResolvedValue(null),
}));

// ------ Dynamic imports (after stubs + mocks) ------
const { useAuthStore } = await import('@/stores/auth-store');

describe('logout IDB cache purge (P1c)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    localStorage.clear();

    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'test-key-32chars-xxxxxxxxxxxx',
      user: null,
      lastError: null,
    });

    mockFetch.mockResolvedValue({ ok: true, status: 200 });
  });

  it('calls clearPersistedQueryCache() on logout', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    expect(mockClearPersistedQueryCache).toHaveBeenCalledOnce();
  });

  it('SW postMessage is still posted on logout (defense-in-depth)', async () => {
    useAuthStore.getState().logout();
    await flushPromises();
    // The existing navigator.serviceWorker?.controller?.postMessage call fires
    // after clearPersistedQueryCache(). It may be called once (the auth-store
    // direct call) or twice (if clearPersistedQueryCache's mock also posts it —
    // but the mock is a no-op here). We assert at least once.
    expect(mockPostMessage).toHaveBeenCalledWith({ type: 'JARVIS_LOGOUT' });
  });

  it('logout still completes when clearPersistedQueryCache rejects', async () => {
    mockClearPersistedQueryCache.mockRejectedValueOnce(new Error('IDB failure'));

    useAuthStore.getState().logout();
    await flushPromises();

    // Auth state must be cleared regardless of IDB purge failure.
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().apiKey).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it('auth state is cleared before IDB purge is awaited (non-blocking)', async () => {
    // Auth state clear is synchronous in logout(); IDB purge is void (non-blocking).
    // We verify auth is already cleared immediately after the synchronous call.
    useAuthStore.getState().logout();

    // Without flushing — synchronous state clear must have happened already.
    expect(useAuthStore.getState().isAuthenticated).toBe(false);

    // IDB purge fires eventually (async) — not required to have fired yet here.
    await flushPromises();
    expect(mockClearPersistedQueryCache).toHaveBeenCalledOnce();
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { SessionUser } from '@/stores/auth-store';
import { createTestQueryClient } from '@/__tests__/test-utils';

// Mock fetch for the API-key→session mint endpoint
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

// Spy on the cross-user-hygiene purges the auth flows fan out to. These are
// mocked at the module boundary so the test asserts the contract (the auth
// flow calls them) without driving real IndexedDB.
const mockClearReviewOutbox = vi.fn().mockResolvedValue(undefined);
const mockClearPersistedQueryCache = vi.fn().mockResolvedValue(undefined);
vi.mock('@/lib/review-outbox', () => ({
  clearReviewOutbox: () => mockClearReviewOutbox(),
}));
vi.mock('@/lib/query-persister', () => ({
  clearPersistedQueryCache: () => mockClearPersistedQueryCache(),
}));

const { useAuthStore, registerQueryClient } = await import('@/stores/auth-store');

// A real QueryClient with spied methods so the purge's cancel→clear path is
// observable. clear() runs synchronously; cancelQueries is fire-and-forget.
const mockQueryClient = createTestQueryClient({});
const mockQueryClientClear = vi.spyOn(mockQueryClient, 'clear');
const mockCancelQueries = vi
  .spyOn(mockQueryClient, 'cancelQueries')
  .mockResolvedValue(undefined);

const flushMicrotasks = async (): Promise<void> => {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  for (let i = 0; i < 20; i++) await Promise.resolve();
};

const OWNER = { id: 7, email: 'owner@example.com', role: 'admin' as const };

// Client-side ceiling mirrors the backend SESSION_TTL (30 days).
const THIRTY_ONE_DAYS_MS = 31 * 24 * 60 * 60 * 1000;

describe('auth-store', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    registerQueryClient(mockQueryClient);
    useAuthStore.setState({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,
      user: null,
      lastError: null,
    });
  });

  it('login mints a session via /api/auth/api-key-session and stores the owner user', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => OWNER,
    });
    const result = await useAuthStore.getState().login('valid-api-key-32chars-long-xxxxx');
    expect(result).toBe(true);
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().authTime).not.toBeNull();
    // Session path: user stored, no raw apiKey persisted (cookie is the credential).
    expect(useAuthStore.getState().getUser()).toEqual(OWNER);
    expect(useAuthStore.getState().getApiKey()).toBeNull();
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/auth/api-key-session',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.objectContaining({ 'X-API-Key': 'valid-api-key-32chars-long-xxxxx' }),
      }),
    );
  });

  it('login with invalid API key does not authenticate and surfaces the error', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: 'Invalid or missing API key' }),
    });
    const result = await useAuthStore.getState().login('wrong-key');
    expect(result).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().getUser()).toBeNull();
    expect(useAuthStore.getState().lastError).toBe('Invalid or missing API key');
  });

  it('login surfaces the 403 owner-recovery message instead of bouncing', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({
        detail: 'API-key recovery is reserved for the configured instance owner; use a passkey or sign-in link',
      }),
    });
    const result = await useAuthStore.getState().login('some-key');
    expect(result).toBe(false);
    expect(useAuthStore.getState().lastError).toContain('passkey or sign-in link');
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('login handles network errors gracefully', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Network error'));
    const result = await useAuthStore.getState().login('some-key');
    expect(result).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().lastError).toContain('Network error');
  });

  it('login still succeeds when localStorage.removeItem throws during the purge', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => OWNER });
    const removeItemSpy = vi
      .spyOn(Storage.prototype, 'removeItem')
      .mockImplementation(() => {
        throw new DOMException('localStorage disabled', 'SecurityError');
      });

    // Before the fix this throws out of loginWithSession → login()'s catch →
    // returns false with a misleading 'Network error' message.
    const result = await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');

    expect(result).toBe(true);
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().getUser()).toEqual(OWNER);
    expect(useAuthStore.getState().lastError).toBeNull();
    removeItemSpy.mockRestore();
  });

  it('logout clears authentication and the session user', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => OWNER });
    await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);

    useAuthStore.getState().logout();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().authTime).toBeNull();
    expect(useAuthStore.getState().getUser()).toBeNull();
  });

  it('session expires after 30 days (isSessionValid pure check + expireSession mutation)', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => OWNER });
    await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');
    expect(useAuthStore.getState().isSessionValid()).toBe(true);

    // Simulate 31 days passing
    const thirtyOneDaysAgo = Date.now() - THIRTY_ONE_DAYS_MS;
    useAuthStore.setState({ authTime: thirtyOneDaysAgo });

    // isSessionValid() is a pure check — returns false but does NOT mutate
    expect(useAuthStore.getState().isSessionValid()).toBe(false);
    // State still unchanged (pure)
    expect(useAuthStore.getState().isAuthenticated).toBe(true);

    // expireSession() does the mutation
    useAuthStore.getState().expireSession();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('a day-old session is still valid (30-day ceiling, not the former 8h)', () => {
    const twentyFourHoursAgo = Date.now() - 24 * 60 * 60 * 1000;
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: twentyFourHoursAgo,
      apiKey: null,
      user: OWNER,
    });
    expect(useAuthStore.getState().isSessionValid()).toBe(true);
  });

  it('isSessionValid returns false when neither apiKey nor user is present', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
      user: null,
    });
    expect(useAuthStore.getState().isSessionValid()).toBe(false);
    // Pure: state unchanged
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  // -----------------------------------------------------------------------
  // isSessionValid() pure predicate + expireSession() side-effect
  // -----------------------------------------------------------------------
  it('isSessionValid returns true for a fresh session without mutating state', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => OWNER });
    await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');
    const authTimeBefore = useAuthStore.getState().authTime;

    const result = useAuthStore.getState().isSessionValid();

    expect(result).toBe(true);
    // Pure: state unchanged
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().authTime).toBe(authTimeBefore);
    expect(useAuthStore.getState().user).toEqual(OWNER);
  });

  it('isSessionValid returns false for an expired session without mutating state', () => {
    const thirtyOneDaysAgo = Date.now() - THIRTY_ONE_DAYS_MS;
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: thirtyOneDaysAgo,
      apiKey: null,
      user: OWNER,
    });

    const result = useAuthStore.getState().isSessionValid();

    expect(result).toBe(false);
    // Pure: isAuthenticated still true — no side-effect happened
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().authTime).toBe(thirtyOneDaysAgo);
  });

  it('isSessionValid returns false when not authenticated', () => {
    // store already reset to unauthenticated by beforeEach
    expect(useAuthStore.getState().isSessionValid()).toBe(false);
    // Still no mutation — remains unauthenticated, not some other falsy state
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('expireSession clears authentication state', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now() - THIRTY_ONE_DAYS_MS,
      apiKey: null,
      user: OWNER,
    });

    useAuthStore.getState().expireSession();

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().authTime).toBeNull();
    expect(useAuthStore.getState().apiKey).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it('logout fans out the cross-user-hygiene purges (FE-A/FE-B)', () => {
    mockFetch.mockResolvedValue({ ok: true, status: 200, json: async () => OWNER });
    useAuthStore.getState().logout();
    // FE-A: review outbox wiped on logout. FE-B: persisted query cache wiped.
    expect(mockClearReviewOutbox).toHaveBeenCalledTimes(1);
    expect(mockClearPersistedQueryCache).toHaveBeenCalledTimes(1);
  });

  it('logout posts JARVIS_LOGOUT directly when a SW controls the page', () => {
    const postMessage = vi.fn();
    const sw = {
      controller: { postMessage },
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal('navigator', { serviceWorker: sw });

    useAuthStore.getState().logout();

    expect(postMessage).toHaveBeenCalledWith({ type: 'JARVIS_LOGOUT' });
    expect(sw.addEventListener).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetch);
  });

  it('logout registers a one-shot controllerchange that posts JARVIS_LOGOUT when controller is null (FE-B race)', () => {
    let captured: (() => void) | null = null;
    const postMessage = vi.fn();
    const sw: {
      controller: { postMessage: typeof postMessage } | null;
      addEventListener: ReturnType<typeof vi.fn>;
      removeEventListener: ReturnType<typeof vi.fn>;
    } = {
      controller: null, // no SW claimed yet — logout right after first install
      addEventListener: vi.fn((evt: string, cb: () => void) => {
        if (evt === 'controllerchange') captured = cb;
      }),
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal('navigator', { serviceWorker: sw });

    useAuthStore.getState().logout();

    // Deferred: nothing posted yet (no controller), listener registered instead.
    expect(postMessage).not.toHaveBeenCalled();
    expect(sw.addEventListener).toHaveBeenCalledWith(
      'controllerchange',
      expect.any(Function),
    );
    expect(captured).toBeTypeOf('function');

    // SW now claims the page → controllerchange fires → purge is posted once,
    // and the one-shot listener removes itself.
    sw.controller = { postMessage };
    captured!();
    expect(postMessage).toHaveBeenCalledWith({ type: 'JARVIS_LOGOUT' });
    expect(sw.removeEventListener).toHaveBeenCalledWith(
      'controllerchange',
      expect.any(Function),
    );

    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetch);
  });

  // -----------------------------------------------------------------------
  // cross-user purge on re-login: session expiry and re-login must run the same cross-user purge
  // fan-out as logout, or the next user on a shared browser inherits the prior
  // user's IndexedDB-persisted + SW-cached private data.
  // -----------------------------------------------------------------------
  it('expireSession purges identity caches (query cache, outbox, query client, SW)', async () => {
    const postMessage = vi.fn();
    vi.stubGlobal('navigator', {
      serviceWorker: {
        controller: { postMessage },
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
    });
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
      user: OWNER,
    });

    useAuthStore.getState().expireSession();

    // Same fan-out as logout — without it the expired session's data leaks.
    expect(mockClearPersistedQueryCache).toHaveBeenCalledTimes(1);
    expect(mockClearReviewOutbox).toHaveBeenCalledTimes(1);
    expect(postMessage).toHaveBeenCalledWith({ type: 'JARVIS_LOGOUT' });
    // Auth fields are still cleared; the purge is additive.
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().user).toBeNull();
    // In-memory cache clear is synchronous; cancel is fire-and-forget.
    expect(mockCancelQueries).toHaveBeenCalledTimes(1);
    expect(mockQueryClientClear).toHaveBeenCalledTimes(1);

    await flushMicrotasks();
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetch);
  });

  it('loginWithSession awaits an acknowledged SW cache clear, then exposes the new identity', async () => {
    const PREV: SessionUser = { id: 1, email: 'prev@example.com', role: 'user' };
    const NEXT: SessionUser = { id: 2, email: 'next@example.com', role: 'user' };
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: null, user: PREV });

    // The SW mock replies on the transferred MessageChannel port (simulating
    // caches.delete completing), which is what unblocks set({...NEXT}).
    let swSeenIdentity: SessionUser | null = null;
    const postMessage = vi.fn((_msg: unknown, transfer?: MessagePort[]) => {
      swSeenIdentity = useAuthStore.getState().user; // store still holds PREV here
      transfer?.[0]?.postMessage({ type: 'JARVIS_LOGOUT_DONE' });
    });
    vi.stubGlobal('navigator', {
      serviceWorker: { controller: { postMessage }, addEventListener: vi.fn(), removeEventListener: vi.fn() },
    });

    let clearIdentity: SessionUser | null = null;
    mockQueryClientClear.mockImplementationOnce(() => { clearIdentity = useAuthStore.getState().user; });

    await useAuthStore.getState().loginWithSession(NEXT);

    expect(useAuthStore.getState().user).toEqual(NEXT);          // exposed only after the await
    expect(swSeenIdentity).toEqual(PREV);                        // SW notified while PREV live
    expect(clearIdentity).toEqual(PREV);                        // in-memory clears ran before set
    expect(postMessage).toHaveBeenCalledWith({ type: 'JARVIS_LOGOUT' }, expect.any(Array));
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetch);
  });

  it('loginWithSession still resolves (and exposes the identity) when no SW controls the page', async () => {
    vi.stubGlobal('navigator', { serviceWorker: { controller: null, addEventListener: vi.fn(), removeEventListener: vi.fn() } });
    await useAuthStore.getState().loginWithSession({ id: 5, email: 'x@y.com', role: 'user' });
    expect(useAuthStore.getState().user).toEqual({ id: 5, email: 'x@y.com', role: 'user' });
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetch);
  });

  it('loginWithSession resolves on timeout when the SW never acks', async () => {
    vi.useFakeTimers();
    const postMessage = vi.fn(); // never replies
    vi.stubGlobal('navigator', { serviceWorker: { controller: { postMessage }, addEventListener: vi.fn(), removeEventListener: vi.fn() } });
    const p = useAuthStore.getState().loginWithSession({ id: 6, email: 'z@z.com', role: 'user' });
    await vi.advanceTimersByTimeAsync(1600);
    await p;
    expect(useAuthStore.getState().user).toEqual({ id: 6, email: 'z@z.com', role: 'user' });
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetch);
  });

  it('loginWithSession awaits IDB cache purge before exposing the new identity', async () => {
    // Hold the IDB purge promise open so we can verify the ordering guarantee:
    // isAuthenticated must remain false until the IDB clear resolves.
    let resolveIdb!: () => void;
    const idbPending = new Promise<void>((resolve) => {
      resolveIdb = resolve;
    });
    mockClearPersistedQueryCache.mockImplementationOnce(() => idbPending);

    const USER: SessionUser = { id: 99, email: 'new@example.com', role: 'user' };
    const loginPromise = useAuthStore.getState().loginWithSession(USER);

    // Drain the microtask queue so loginWithSession runs as far as it can
    // without the IDB promise resolving (past the SW-clear await which is a
    // no-op — no SW controller in the test environment).
    await flushMicrotasks();

    // IDB purge still pending — the new identity must NOT be exposed yet.
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().user).toBeNull();

    // Unblock the IDB clear and let loginWithSession finish.
    resolveIdb();
    await loginPromise;

    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().user).toEqual(USER);
  });

  // -----------------------------------------------------------------------
  // hydrateFromCookie — restores the CURRENT identity from a valid session
  // cookie (new tab / empty sessionStorage). No identity switch, so it must
  // NOT run the cross-user purge fan-out that loginWithSession runs.
  // -----------------------------------------------------------------------
  it('hydrateFromCookie sets the session fields synchronously', () => {
    useAuthStore.getState().hydrateFromCookie(OWNER);

    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().authTime).not.toBeNull();
    expect(useAuthStore.getState().user).toEqual(OWNER);
    expect(useAuthStore.getState().apiKey).toBeNull();
    expect(useAuthStore.getState().lastError).toBeNull();
  });

  it('hydrateFromCookie does NOT trigger the identity-cache purge fan-out', async () => {
    const postMessage = vi.fn();
    vi.stubGlobal('navigator', {
      serviceWorker: {
        controller: { postMessage },
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
    });

    useAuthStore.getState().hydrateFromCookie(OWNER);
    await flushMicrotasks();

    // Restoring an identity, not switching users: nothing may be purged.
    expect(mockClearPersistedQueryCache).not.toHaveBeenCalled();
    expect(mockClearReviewOutbox).not.toHaveBeenCalled();
    expect(mockCancelQueries).not.toHaveBeenCalled();
    expect(mockQueryClientClear).not.toHaveBeenCalled();
    expect(postMessage).not.toHaveBeenCalled();

    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetch);
  });

  it('loginWithSession completes even if the IDB purge never settles', async () => {
    // A stuck IndexedDB transaction (e.g. another tab holds the DB open) must not
    // block login forever: the awaited purge is bounded by a timeout.
    vi.useFakeTimers();
    try {
      mockClearPersistedQueryCache.mockImplementationOnce(
        () => new Promise<void>(() => {}),
      );
      const USER: SessionUser = { id: 7, email: 'stuck@example.com', role: 'user' };
      const loginPromise = useAuthStore.getState().loginWithSession(USER);

      // Past the IDB bound (2000ms) and any SW-clear bound (1500ms).
      await vi.advanceTimersByTimeAsync(4000);
      await loginPromise;

      expect(useAuthStore.getState().isAuthenticated).toBe(true);
      expect(useAuthStore.getState().user).toEqual(USER);
    } finally {
      vi.useRealTimers();
    }
  });
});

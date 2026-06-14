import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock fetch for the API-key→session mint endpoint
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

// Spy on the cross-user-hygiene purges logout fans out to. These are mocked at
// the module boundary so the test asserts the contract (logout calls them)
// without driving real IndexedDB.
const mockClearReviewOutbox = vi.fn().mockResolvedValue(undefined);
const mockClearPersistedQueryCache = vi.fn().mockResolvedValue(undefined);
vi.mock('@/lib/review-outbox', () => ({
  clearReviewOutbox: () => mockClearReviewOutbox(),
}));
vi.mock('@/lib/query-persister', () => ({
  clearPersistedQueryCache: () => mockClearPersistedQueryCache(),
}));

const { useAuthStore } = await import('@/stores/auth-store');

const OWNER = { id: 7, email: 'owner@example.com', role: 'admin' as const };

describe('auth-store', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it('login surfaces the 403 multi-tenant-disabled message instead of bouncing', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({
        detail: 'API-key login disabled for multi-tenant deployments; use magic-link',
      }),
    });
    const result = await useAuthStore.getState().login('some-key');
    expect(result).toBe(false);
    expect(useAuthStore.getState().lastError).toContain('magic-link');
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('login handles network errors gracefully', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Network error'));
    const result = await useAuthStore.getState().login('some-key');
    expect(result).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().lastError).toContain('Network error');
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

  it('session expires after 8 hours (isSessionValid pure check + expireSession mutation)', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => OWNER });
    await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');
    expect(useAuthStore.getState().isSessionValid()).toBe(true);

    // Simulate 9 hours passing
    const nineHoursAgo = Date.now() - 9 * 60 * 60 * 1000;
    useAuthStore.setState({ authTime: nineHoursAgo });

    // isSessionValid() is a pure check — returns false but does NOT mutate
    expect(useAuthStore.getState().isSessionValid()).toBe(false);
    // State still unchanged (pure)
    expect(useAuthStore.getState().isAuthenticated).toBe(true);

    // expireSession() does the mutation
    useAuthStore.getState().expireSession();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
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
    const nineHoursAgo = Date.now() - 9 * 60 * 60 * 1000;
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: nineHoursAgo,
      apiKey: null,
      user: OWNER,
    });

    const result = useAuthStore.getState().isSessionValid();

    expect(result).toBe(false);
    // Pure: isAuthenticated still true — no side-effect happened
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().authTime).toBe(nineHoursAgo);
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
      authTime: Date.now() - 9 * 60 * 60 * 1000,
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
});

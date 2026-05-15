import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock fetch for the API-key→session mint endpoint
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

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

  it('session expires after 8 hours', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => OWNER });
    await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');
    expect(useAuthStore.getState().checkSession()).toBe(true);

    // Simulate 9 hours passing
    const nineHoursAgo = Date.now() - 9 * 60 * 60 * 1000;
    useAuthStore.setState({ authTime: nineHoursAgo });

    expect(useAuthStore.getState().checkSession()).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('checkSession returns false when neither apiKey nor user is present', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
      user: null,
    });
    expect(useAuthStore.getState().checkSession()).toBe(false);
  });
});

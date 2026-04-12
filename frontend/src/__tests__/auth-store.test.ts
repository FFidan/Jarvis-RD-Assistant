import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock fetch for API-key-based login
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

const { useAuthStore } = await import('@/stores/auth-store');

describe('auth-store', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,
    });
  });

  it('login with valid API key sets authenticated', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200 });
    const result = await useAuthStore.getState().login('valid-api-key-32chars-long-xxxxx');
    expect(result).toBe(true);
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().authTime).not.toBeNull();
    expect(useAuthStore.getState().getApiKey()).toBe('valid-api-key-32chars-long-xxxxx');
    // Verify fetch was called with correct headers
    expect(mockFetch).toHaveBeenCalledWith('/api/topics', {
      headers: { 'X-API-Key': 'valid-api-key-32chars-long-xxxxx' },
    });
  });

  it('login with invalid API key does not authenticate', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 403 });
    const result = await useAuthStore.getState().login('wrong-key');
    expect(result).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().getApiKey()).toBeNull();
  });

  it('login handles network errors gracefully', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Network error'));
    const result = await useAuthStore.getState().login('some-key');
    expect(result).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('logout clears authentication and API key', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200 });
    await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);

    useAuthStore.getState().logout();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().authTime).toBeNull();
    expect(useAuthStore.getState().getApiKey()).toBeNull();
  });

  it('session expires after 8 hours', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200 });
    await useAuthStore.getState().login('valid-key-32chars-xxxxxxxxxx');
    expect(useAuthStore.getState().checkSession()).toBe(true);

    // Simulate 9 hours passing
    const nineHoursAgo = Date.now() - 9 * 60 * 60 * 1000;
    useAuthStore.setState({ authTime: nineHoursAgo });

    expect(useAuthStore.getState().checkSession()).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('checkSession returns false when apiKey is null', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
    });
    expect(useAuthStore.getState().checkSession()).toBe(false);
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';

// We need to mock import.meta.env before importing the store
vi.stubEnv('VITE_DASHBOARD_PASSWORD', 'test-password');

// Dynamic import to ensure env is mocked before module loads
const { useAuthStore } = await import('@/stores/auth-store');

describe('auth-store', () => {
  beforeEach(() => {
    // Reset store state
    useAuthStore.setState({
      isAuthenticated: false,
      authTime: null,
    });
  });

  it('login with correct password sets authenticated', () => {
    const result = useAuthStore.getState().login('test-password');
    expect(result).toBe(true);
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().authTime).not.toBeNull();
  });

  it('login with wrong password does not authenticate', () => {
    const result = useAuthStore.getState().login('wrong');
    expect(result).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('logout clears authentication', () => {
    useAuthStore.getState().login('test-password');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);

    useAuthStore.getState().logout();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().authTime).toBeNull();
  });

  it('session expires after 8 hours', () => {
    useAuthStore.getState().login('test-password');
    expect(useAuthStore.getState().checkSession()).toBe(true);

    // Simulate 9 hours passing
    const nineHoursAgo = Date.now() - 9 * 60 * 60 * 1000;
    useAuthStore.setState({ authTime: nineHoursAgo });

    expect(useAuthStore.getState().checkSession()).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });
});

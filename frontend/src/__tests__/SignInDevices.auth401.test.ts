/**
 * Revoked-mid-session wiring.
 *
 * When an admin revokes a user's passkeys/sessions while they're active, the
 * user's next passkey request 401s. Passkey API calls are thin `apiFetch`
 * wrappers, so that 401 flows through the shared auto-logout in `lib/api/core`
 * (`handleAuthFailure` → toast + `logout()`) — no bespoke handling in the
 * passkey UI. This exercises the real functions against a mocked 401 response,
 * mirroring the existing api-auth-debounce spec.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

const logoutMock = vi.fn();
// Mutable so a test can flip the session between "signed in" (auto-logout fires)
// and "signed out" (an unauthenticated login: handleAuthFailure must NOT fire).
const { authState } = vi.hoisted(() => ({ authState: { isAuthenticated: true } }));
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: () => ({
      getApiKey: () => 'test-key',
      isAuthenticated: authState.isAuthenticated,
      logout: logoutMock,
    }),
  },
}));

import { beginPasskeyLogin, listPasskeys, deletePasskey, ApiError } from '@/lib/api';
import { toast } from 'sonner';

describe('passkey calls route a 401 through the shared auto-logout', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    logoutMock.mockClear();
    vi.mocked(toast.error).mockClear();
    authState.isAuthenticated = true;
  });

  it('logs the user out and rejects when the passkey list 401s', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('unauthorized', { status: 401 }),
    );

    await expect(listPasskeys()).rejects.toBeInstanceOf(ApiError);
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringMatching(/session expired/i),
      expect.objectContaining({ duration: 6000 }),
    );
  });

  it('logs the user out when revoking a passkey 401s', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('unauthorized', { status: 401 }),
    );

    await expect(deletePasskey('cred-1')).rejects.toBeInstanceOf(ApiError);
    expect(logoutMock).toHaveBeenCalled();
  });
});

describe('an unauthenticated passkey login does not log the visitor out on 401', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    logoutMock.mockClear();
    vi.mocked(toast.error).mockClear();
    authState.isAuthenticated = false;
  });

  it('rejects with ApiError without touching the (absent) session', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('unauthorized', { status: 401 }),
    );

    // A failed login begin 401s; handleAuthFailure early-returns when not
    // authenticated, so the login button surfaces an inline error (classified
    // ApiError) instead of bouncing the visitor through a logout.
    await expect(beginPasskeyLogin()).rejects.toBeInstanceOf(ApiError);
    expect(logoutMock).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });
});

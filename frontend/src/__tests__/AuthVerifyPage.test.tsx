/**
 * Tests for AuthVerifyPage — magic-link verification handler.
 *
 * Scope:
 * - Success: posts the token, calls loginWithSession, navigates to "/".
 * - Failure: shows the error and (eventually) navigates to /login?error=...
 * - Missing token: redirects straight to /login?error=Missing+token.
 * - Timer cleanup: unmounting within the 2 s error-redirect window cancels the
 *   timer so navigate is NOT called on an unmounted component (L-4 fix).
 */
import { StrictMode } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthVerifyPage, __resetVerifyDedupeForTests } from '@/pages/AuthVerifyPage';

const verifyMock = vi.fn();
const loginWithSessionMock = vi.fn();

vi.mock('@/lib/api', () => ({
  verifyMagicLink: (token: string) => verifyMock(token),
}));

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: () => ({
    loginWithSession: loginWithSessionMock,
  }),
}));

function renderWithRoute(initialUrl: string) {
  return render(
    <MemoryRouter initialEntries={[initialUrl]}>
      <Routes>
        <Route path="/auth/verify" element={<AuthVerifyPage />} />
        <Route path="/" element={<div>HOME</div>} />
        <Route path="/login" element={<div>LOGIN</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AuthVerifyPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetVerifyDedupeForTests();
  });

  it('on success, calls loginWithSession with returned user and navigates to /', async () => {
    verifyMock.mockResolvedValueOnce({ id: 7, email: 'a@b.com', role: 'admin' });
    renderWithRoute('/auth/verify?token=abcdefghijklmnop');

    await waitFor(() => {
      expect(verifyMock).toHaveBeenCalledWith('abcdefghijklmnop');
    });
    await waitFor(() => {
      expect(loginWithSessionMock).toHaveBeenCalledWith({
        id: 7,
        email: 'a@b.com',
        role: 'admin',
      });
    });
    await waitFor(() => {
      expect(screen.getByText('HOME')).toBeInTheDocument();
    });
  });

  it('on failure, shows error then navigates to /login', async () => {
    verifyMock.mockRejectedValueOnce(new Error('Invalid or expired token'));
    renderWithRoute('/auth/verify?token=expired-token-aaaa');

    expect(await screen.findByText(/invalid or expired token/i)).toBeInTheDocument();
    await waitFor(
      () => {
        expect(screen.getByText('LOGIN')).toBeInTheDocument();
      },
      { timeout: 4000 },
    );
  });

  it('without a token, redirects straight to /login', async () => {
    renderWithRoute('/auth/verify');
    await waitFor(() => {
      expect(screen.getByText('LOGIN')).toBeInTheDocument();
    });
    expect(verifyMock).not.toHaveBeenCalled();
  });

  it('under StrictMode, signs in without sticking on "Verifying" (single POST)', async () => {
    // mockResolvedValue (not Once): the deduped promise is awaited by both mounts.
    verifyMock.mockResolvedValue({ id: 7, email: 'a@b.com', role: 'admin' });
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/auth/verify?token=strict-mode-token']}>
          <Routes>
            <Route path="/auth/verify" element={<AuthVerifyPage />} />
            <Route path="/" element={<div>HOME</div>} />
            <Route path="/login" element={<div>LOGIN</div>} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    );
    await waitFor(() => expect(screen.getByText('HOME')).toBeInTheDocument());
    expect(verifyMock).toHaveBeenCalledTimes(1);
  });

  describe('timer cleanup (L-4)', () => {
    beforeEach(() => {
      // shouldAdvanceTime keeps @testing-library's internal polling working
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('unmounting within 2 s cancels the redirect timer (navigate not called)', async () => {
      verifyMock.mockRejectedValueOnce(new Error('Bad token'));
      const { unmount } = renderWithRoute('/auth/verify?token=bad-token-xxx');

      // Wait for the error state to be set (async catch block ran)
      await waitFor(() => {
        expect(screen.queryByText(/bad token/i)).toBeInTheDocument();
      });

      // Unmount before the 2 s timer fires — cleanup should clearTimeout
      unmount();

      // Advance past the 2 s window; navigate must NOT have been called
      await act(async () => {
        vi.advanceTimersByTime(3000);
      });

      // LOGIN route should never have been rendered (component is gone)
      expect(screen.queryByText('LOGIN')).not.toBeInTheDocument();
    });

    it('on failure, navigates to /login after the 2 s delay when not unmounted', async () => {
      verifyMock.mockRejectedValueOnce(new Error('Expired link'));
      renderWithRoute('/auth/verify?token=expired-xxx');

      // Wait for error UI to appear (timer is now running)
      await waitFor(() => {
        expect(screen.queryByText(/expired link/i)).toBeInTheDocument();
      });

      // Advance past the 2 s redirect delay — LOGIN route must now render
      await act(async () => {
        vi.advanceTimersByTime(2500);
      });
      await waitFor(() => {
        expect(screen.getByText('LOGIN')).toBeInTheDocument();
      });
    });
  });
});

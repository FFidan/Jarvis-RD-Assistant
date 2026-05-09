/**
 * Tests for AuthVerifyPage — Phase 2 WS-2A magic-link verification handler.
 *
 * Scope:
 * - Success: posts the token, calls loginWithSession, navigates to "/".
 * - Failure: shows the error and (eventually) navigates to /login?error=...
 * - Missing token: redirects straight to /login?error=Missing+token.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthVerifyPage } from '@/pages/AuthVerifyPage';

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
});

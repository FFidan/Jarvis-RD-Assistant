/**
 * Tests for LoginPage — Phase 2 WS-2A magic-link login surface.
 *
 * Scope:
 * - Magic-link form is the default mode and submits to requestMagicLink.
 * - "Use API key instead" toggle reveals the API-key form (legacy path).
 * - The legacy API-key form preserves the old autoComplete contract (FE-006).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { LoginPage } from '@/pages/LoginPage';

const requestMagicLinkMock = vi.fn().mockResolvedValue({ sent: true });
const loginMock = vi.fn().mockResolvedValue(false);

vi.mock('@/lib/api', () => ({
  requestMagicLink: (email: string) => requestMagicLinkMock(email),
}));

let storeLastError: string | null = null;

vi.mock('@/stores/auth-store', () => {
  const useAuthStore = () => ({ login: loginMock });
  // LoginPage reads useAuthStore.getState().lastError after a failed login to
  // surface the precise backend message (e.g. the 403 multi-tenant message).
  useAuthStore.getState = () => ({ lastError: storeLastError });
  return { useAuthStore };
});

function renderLoginPage() {
  return render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    storeLastError = null;
    loginMock.mockResolvedValue(false);
  });

  it('renders the backend 403 multi-tenant-disabled message instead of bouncing', async () => {
    const user = userEvent.setup();
    storeLastError =
      'API-key login disabled for multi-tenant deployments; use magic-link';
    loginMock.mockResolvedValueOnce(false);
    renderLoginPage();

    await user.click(screen.getByRole('button', { name: /use api key instead/i }));
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
    await user.type(input, 'some-api-key-value');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByText(/use magic-link/i)).toBeInTheDocument();
  });

  it('renders the magic-link email input by default', () => {
    renderLoginPage();
    const input = screen.getByPlaceholderText(/you@example\.com/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'email');
  });

  it('submits the email to requestMagicLink and shows confirmation', async () => {
    const user = userEvent.setup();
    renderLoginPage();
    const input = screen.getByPlaceholderText(/you@example\.com/i);
    await user.type(input, 'ferhat@example.com');
    await user.click(screen.getByRole('button', { name: /send magic link/i }));

    await waitFor(() => {
      expect(requestMagicLinkMock).toHaveBeenCalledWith('ferhat@example.com');
    });
    expect(await screen.findByText(/Check your email/i)).toBeInTheDocument();
  });

  it('toggles to the API-key form on click', async () => {
    const user = userEvent.setup();
    renderLoginPage();

    await user.click(screen.getByRole('button', { name: /use api key instead/i }));
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'password');
  });

  it('legacy API-key input preserves autoComplete="current-password"', async () => {
    const user = userEvent.setup();
    renderLoginPage();
    await user.click(screen.getByRole('button', { name: /use api key instead/i }));
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
    expect(input).toHaveAttribute('autocomplete', 'current-password');
  });

  it('shows initial error from query param', () => {
    render(
      <MemoryRouter initialEntries={['/login?error=Invalid+token']}>
        <LoginPage />
      </MemoryRouter>,
    );
    expect(screen.getByText(/invalid token/i)).toBeInTheDocument();
  });
});

/**
 * Tests for LoginPage — magic-link login surface, SMTP-aware.
 *
 * Scope:
 * - smtp_configured=false, single mode → API-key form is the default tab;
 *   magic-link tab still reachable but shows an SMTP-unconfigured notice.
 * - smtp_configured=false, multi mode → magic-link form is the default tab
 *   with an honest notice that links cannot be delivered; API-key tab still
 *   reachable but the server rejects it once more than one account exists.
 * - setup_mode and smtp_configured absent -> magic-link
 *   is the default (safe fallback, no behavior change).
 * - Multi-user API-key copy identifies the configured owner's recovery path.
 * - 422 from requestMagicLink surfaces as an email-validation message.
 * - The page renders a <main> landmark.
 * - Errors are announced via role="alert".
 * - The API-key input opts out of browser credential storage.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LoginPage } from '@/pages/LoginPage';

const requestMagicLinkMock = vi.fn().mockResolvedValue({ sent: true });
const getFirstRunStatusMock = vi.fn();
const loginMock = vi.fn().mockResolvedValue(false);

// Passkey seams — the capability probe + login ceremony.
const getPasskeyCapabilityMock = vi.fn();
const beginPasskeyLoginMock = vi.fn();
const finishPasskeyLoginMock = vi.fn();
const startAuthenticationMock = vi.fn();
const browserSupportsWebAuthnMock = vi.fn().mockReturnValue(false);
const loginWithSessionMock = vi.fn();

// Minimal stand-in for the library's WebAuthnError so `instanceof` in the hook
// (which imports the SAME mocked module) matches user-cancel / timeout errors.
// Hoisted so the (synchronous) module-mock factory can reference it eagerly.
const { MockWebAuthnError } = vi.hoisted(() => {
  class MockWebAuthnError extends Error {
    code: string;
    // Default to the code the library emits for a user cancel / ceremony timeout
    // (a passed-through NotAllowedError), not the abort-signal code.
    constructor(code = 'ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY') {
      super('webauthn ceremony error');
      this.name = 'WebAuthnError';
      this.code = code;
    }
  }
  return { MockWebAuthnError };
});

vi.mock('@simplewebauthn/browser', () => ({
  browserSupportsWebAuthn: () => browserSupportsWebAuthnMock(),
  startAuthentication: (opts: unknown) => startAuthenticationMock(opts),
  startRegistration: vi.fn(),
  WebAuthnError: MockWebAuthnError,
}));

vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  requestMagicLink: (email: string) => requestMagicLinkMock(email),
  getFirstRunStatus: () => getFirstRunStatusMock(),
  getPasskeyCapability: () => getPasskeyCapabilityMock(),
  beginPasskeyLogin: () => beginPasskeyLoginMock(),
  finishPasskeyLogin: (assertion: unknown) => finishPasskeyLoginMock(assertion),
}));

let storeLastError: string | null = null;

vi.mock('@/stores/auth-store', () => {
  const useAuthStore = () => ({ login: loginMock });
  // LoginPage reads useAuthStore.getState().lastError after a failed login to
  // surface the precise backend message (e.g. the 403 multi-tenant message);
  // the passkey login path calls getState().loginWithSession(user).
  useAuthStore.getState = () => ({
    lastError: storeLastError,
    getApiKey: () => null,
    isAuthenticated: false,
    loginWithSession: loginWithSessionMock,
  });
  return { useAuthStore };
});

function makeQC() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

/** Render LoginPage with an optional mocked first-run status response. */
function renderLoginPage(firstRunData?: object | null) {
  const qc = makeQC();
  const response = firstRunData === undefined ? { configured: true } : firstRunData;
  if (response !== null) {
    getFirstRunStatusMock.mockResolvedValue(response);
  } else {
    getFirstRunStatusMock.mockRejectedValue(new Error('setup status unavailable'));
  }
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <LoginPage />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getFirstRunStatusMock.mockReset();
    storeLastError = null;
    loginMock.mockResolvedValue(false);
    // Passkeys off by default (jsdom has no WebAuthn); capable tests opt in.
    browserSupportsWebAuthnMock.mockReturnValue(false);
    getPasskeyCapabilityMock.mockReset();
    beginPasskeyLoginMock.mockReset();
    finishPasskeyLoginMock.mockReset();
    startAuthenticationMock.mockReset();
    localStorage.clear();
  });

  // ---------------------------------------------------------------------------
  // Default tab conditioning on smtp_configured
  // ---------------------------------------------------------------------------

  it('defaults to the API-key tab when saved setup_mode=single even if SMTP is configured', async () => {
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'single' });

    const input = await screen.findByPlaceholderText(/Your API key/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'password');
  });

  it('renders the magic-link email input by default when saved setup_mode=multi', async () => {
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

    const input = await screen.findByPlaceholderText(/you@example\.com/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'email');
  });

  it('defaults to the API-key tab in single mode when smtp_configured=false', async () => {
    // Pre-seed cache: SMTP not configured, single mode → API-key should be primary.
    renderLoginPage({ configured: true, smtp_configured: false, setup_mode: 'single' });
    // useEffect fires after render to flip mode.
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/Your API key/i)).toBeInTheDocument();
    });
    const input = screen.getByPlaceholderText(/Your API key/i);
    expect(input).toHaveAttribute('type', 'password');
  });

  it('stays on magic-link tab in multi mode when smtp_configured=false', () => {
    // Multi mode without SMTP: magic-link stays primary so the honest notice is visible.
    // API-key login is rejected by the backend once >1 account exists.
    renderLoginPage({ configured: true, smtp_configured: false, setup_mode: 'multi' });
    expect(screen.getByPlaceholderText(/you@example\.com/i)).toBeInTheDocument();
  });

  it('shows an SMTP-unconfigured notice on the magic-link tab in single mode when smtp_configured=false', async () => {
    renderLoginPage({ configured: true, smtp_configured: false, setup_mode: 'single' });
    // Wait for the effect to flip to api-key, then switch back.
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/Your API key/i)).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: /use magic link instead/i }));
    await waitFor(() => {
      expect(screen.getByRole('status')).toBeInTheDocument();
    });
    expect(screen.getByRole('status')).toHaveTextContent(/email is not configured/i);
  });

  it('shows multi-user SMTP notice without "use API key" suggestion when smtp_configured=false and multi mode', async () => {
    // In multi mode the magic-link tab is primary; notice must NOT suggest API-key
    // as a working path because the backend rejects it once >1 account exists.
    renderLoginPage({ configured: true, smtp_configured: false, setup_mode: 'multi' });
    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent(/ask your admin/i);
    expect(notice).not.toHaveTextContent(/use the api key tab/i);
  });

  it('defaults to magic-link when setup status omits mode and SMTP state', async () => {
    renderLoginPage({ configured: true });
    expect(await screen.findByPlaceholderText(/you@example\.com/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Magic-link submission
  // ---------------------------------------------------------------------------

  it('submits the email to requestMagicLink and shows confirmation', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
    const input = await screen.findByPlaceholderText(/you@example\.com/i);
    await user.type(input, 'ferhat@example.com');
    await user.click(screen.getByRole('button', { name: /send magic link/i }));

    await waitFor(() => {
      expect(requestMagicLinkMock).toHaveBeenCalledWith('ferhat@example.com');
    });
    expect(await screen.findByText(/a sign-in link is on its way/i)).toBeInTheDocument();
  });

  it('points at the admin sign-in link when SMTP is off', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: false, setup_mode: 'multi' });
    const input = await screen.findByPlaceholderText(/you@example\.com/i);
    await user.type(input, 'ferhat@example.com');
    await user.click(screen.getByRole('button', { name: /send magic link/i }));

    await waitFor(() => {
      expect(requestMagicLinkMock).toHaveBeenCalledWith('ferhat@example.com');
    });
    // Honest when email can't deliver: the link exists; the admin can share it —
    // still enumeration-safe ("If an account exists…"), never "email is configured".
    const confirmation = await screen.findByText(/ask your admin to share your sign-in link/i);
    expect(confirmation).toBeInTheDocument();
    expect(confirmation).toHaveTextContent(/if an account exists/i);
  });

  it('shows the static cooldown hint on the magic-link tab', async () => {
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
    // Static, no per-account data — leaks nothing, always present pre-submit.
    expect(await screen.findByText(/only one link is sent per 2 minutes/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // SMTP configured-but-failing notice (smtp_reachable === false)
  // ---------------------------------------------------------------------------

  it('shows the failing-relay notice when SMTP is configured but not reachable', async () => {
    renderLoginPage({
      configured: true,
      smtp_configured: true,
      smtp_reachable: false,
      setup_mode: 'multi',
    });
    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent(/mail server is not responding/i);
  });

  it('does not show the failing-relay notice when SMTP is reachable', async () => {
    renderLoginPage({
      configured: true,
      smtp_configured: true,
      smtp_reachable: true,
      setup_mode: 'multi',
    });
    await screen.findByPlaceholderText(/you@example\.com/i);
    expect(screen.queryByText(/mail server is not responding/i)).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Toggle behaviour
  // ---------------------------------------------------------------------------

  it('labels API-key login as owner recovery in multi-user mode', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

    await user.click(await screen.findByRole('button', { name: /instance owner recovery/i }));
    const input = screen.getByPlaceholderText(/Your API key/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'password');
    expect(screen.getByText(/only the configured instance owner/i)).toBeInTheDocument();
    expect(screen.getByText(/family members use a passkey or sign-in link/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Backend error propagation
  // ---------------------------------------------------------------------------

  it('renders the backend 403 multi-tenant-disabled message instead of bouncing', async () => {
    const user = userEvent.setup();
    storeLastError =
      'API-key recovery is reserved for the configured instance owner; use a passkey or sign-in link';
    loginMock.mockResolvedValueOnce(false);
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

    await user.click(await screen.findByRole('button', { name: /instance owner recovery/i }));
    const input = screen.getByPlaceholderText(/Your API key/i);
    await user.type(input, 'some-api-key-value');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/use a passkey or sign-in link/i);
  });

  // ---------------------------------------------------------------------------
  // Accessibility / misc
  // ---------------------------------------------------------------------------

  it('API-key input opts out of browser credential storage', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
    await user.click(await screen.findByRole('button', { name: /instance owner recovery/i }));
    const input = screen.getByPlaceholderText(/Your API key/i);
    expect(input).toHaveAttribute('autocomplete', 'off');
  });

  it('shows initial error from query param', () => {
    const qc = makeQC();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/login?error=Invalid+token']}>
          <LoginPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText(/invalid token/i)).toBeInTheDocument();
  });

  it('maps a 422 from request-link to an email-validation message, not a connection error', async () => {
    const { ApiError } = await import('@/lib/api');
    requestMagicLinkMock.mockRejectedValueOnce(new ApiError(422, '{"detail":"value is not a valid email address"}'));
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
    await userEvent.type(await screen.findByLabelText('Email'), 'not-an-email@x');
    await userEvent.click(screen.getByRole('button', { name: /send magic link/i }));
    expect(await screen.findByText(/valid email/i)).toBeInTheDocument();
    expect(screen.queryByText(/check your connection/i)).not.toBeInTheDocument();
  });

  it('renders a <main> landmark', () => {
    renderLoginPage();
    expect(screen.getByRole('main')).toBeInTheDocument();
  });

  it('announces email-required error via role="alert"', async () => {
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
    await userEvent.click(await screen.findByRole('button', { name: /send magic link/i }));
    expect(screen.getByRole('alert')).toHaveTextContent(/email is required/i);
  });

  // ---------------------------------------------------------------------------
  // Passkey sign-in (progressive enhancement)
  // ---------------------------------------------------------------------------

  describe('passkey sign-in', () => {
    /** Turn on browser + server capability for a "passkeys work here" render. */
    function capableHere(mode = 'localhost') {
      browserSupportsWebAuthnMock.mockReturnValue(true);
      getPasskeyCapabilityMock.mockResolvedValue({ available: true, access_mode: mode });
    }

    it('renders an honest note (no button) when the browser lacks WebAuthn', async () => {
      renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
      expect(
        await screen.findByText(/browser does not support passkeys/i),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /sign in with a passkey/i }),
      ).not.toBeInTheDocument();
    });

    it('shows the LAN one-liner (no button) when the server says passkeys are unavailable here', async () => {
      browserSupportsWebAuthnMock.mockReturnValue(true);
      getPasskeyCapabilityMock.mockResolvedValue({ available: false, access_mode: 'lan' });
      renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
      expect(
        await screen.findByText(/passkeys work on the jarvis computer itself/i),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /sign in with a passkey/i }),
      ).not.toBeInTheDocument();
    });

    it('completes a passkey login and hands the user to loginWithSession', async () => {
      const user = userEvent.setup();
      capableHere();
      beginPasskeyLoginMock.mockResolvedValue({ challenge: 'x' });
      startAuthenticationMock.mockResolvedValue({ id: 'assertion' });
      finishPasskeyLoginMock.mockResolvedValue({ id: 7, email: 'a@b.co', role: 'user' });
      renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

      await user.click(await screen.findByRole('button', { name: /sign in with a passkey/i }));

      await waitFor(() =>
        expect(finishPasskeyLoginMock).toHaveBeenCalledWith({ id: 'assertion' }),
      );
      await waitFor(() =>
        expect(loginWithSessionMock).toHaveBeenCalledWith({ id: 7, email: 'a@b.co', role: 'user' }),
      );
    });

    it('shows a cancelled/timeout alert and retries the ceremony on "Try again"', async () => {
      const user = userEvent.setup();
      capableHere();
      beginPasskeyLoginMock.mockResolvedValue({ challenge: 'x' });
      startAuthenticationMock.mockRejectedValue(
        new MockWebAuthnError('ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY'),
      );
      renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

      await user.click(await screen.findByRole('button', { name: /sign in with a passkey/i }));
      expect(await screen.findByRole('alert')).toHaveTextContent(/cancelled or timed out/i);

      startAuthenticationMock.mockResolvedValue({ id: 'assertion' });
      finishPasskeyLoginMock.mockResolvedValue({ id: 7, email: 'a@b.co', role: 'user' });
      await user.click(screen.getByRole('button', { name: /try again/i }));
      await waitFor(() => expect(beginPasskeyLoginMock).toHaveBeenCalledTimes(2));
    });

    it('reports a config fault honestly instead of blaming a cancel', async () => {
      const user = userEvent.setup();
      capableHere();
      beginPasskeyLoginMock.mockResolvedValue({ challenge: 'x' });
      // A SecurityError → ERROR_INVALID_RP_ID: a deployment/config fault, not a cancel.
      startAuthenticationMock.mockRejectedValue(new MockWebAuthnError('ERROR_INVALID_RP_ID'));
      renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

      await user.click(await screen.findByRole('button', { name: /sign in with a passkey/i }));
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent(/complete passkey sign-in here/i);
      expect(alert).not.toHaveTextContent(/cancelled or timed out/i);
    });

    it('surfaces an expired-request message when finish rejects with 400', async () => {
      const { ApiError } = await import('@/lib/api');
      const user = userEvent.setup();
      capableHere();
      beginPasskeyLoginMock.mockResolvedValue({ challenge: 'x' });
      startAuthenticationMock.mockResolvedValue({ id: 'assertion' });
      finishPasskeyLoginMock.mockRejectedValue(
        new ApiError(400, '{"detail":"challenge expired"}'),
      );
      renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

      await user.click(await screen.findByRole('button', { name: /sign in with a passkey/i }));
      expect(await screen.findByRole('alert')).toHaveTextContent(/expired/i);
    });
  });
});

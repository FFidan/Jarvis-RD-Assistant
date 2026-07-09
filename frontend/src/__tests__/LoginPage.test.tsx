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
 * - "Use API key instead" toggle reveals the API-key form.
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

vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  requestMagicLink: (email: string) => requestMagicLinkMock(email),
  getFirstRunStatus: () => getFirstRunStatusMock(),
}));

let storeLastError: string | null = null;

vi.mock('@/stores/auth-store', () => {
  const useAuthStore = () => ({ login: loginMock });
  // LoginPage reads useAuthStore.getState().lastError after a failed login to
  // surface the precise backend message (e.g. the 403 multi-tenant message).
  useAuthStore.getState = () => ({ lastError: storeLastError, getApiKey: () => null, isAuthenticated: false });
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
    expect(await screen.findByText(/will be delivered when email is configured/i)).toBeInTheDocument();
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

  it('toggles to the API-key form on click', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

    await user.click(screen.getByRole('button', { name: /use api key instead/i }));
    const input = screen.getByPlaceholderText(/Your API key/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'password');
  });

  // ---------------------------------------------------------------------------
  // Backend error propagation
  // ---------------------------------------------------------------------------

  it('renders the backend 403 multi-tenant-disabled message instead of bouncing', async () => {
    const user = userEvent.setup();
    storeLastError =
      'API-key login disabled for multi-tenant deployments; use magic-link';
    loginMock.mockResolvedValueOnce(false);
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });

    await user.click(screen.getByRole('button', { name: /use api key instead/i }));
    const input = screen.getByPlaceholderText(/Your API key/i);
    await user.type(input, 'some-api-key-value');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByText(/use magic-link/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Accessibility / misc
  // ---------------------------------------------------------------------------

  it('API-key input opts out of browser credential storage', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'multi' });
    await user.click(await screen.findByRole('button', { name: /use api key instead/i }));
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
});

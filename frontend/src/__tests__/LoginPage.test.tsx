/**
 * Tests for LoginPage — magic-link login surface, SMTP-aware.
 *
 * Scope:
 * - smtp_configured=true  → magic-link form is the default tab.
 * - smtp_configured=false, single mode → API-key form is the default tab;
 *   magic-link tab still reachable but shows an SMTP-unconfigured notice.
 * - smtp_configured=false, multi mode → magic-link form is the default tab
 *   with an honest notice that links cannot be delivered; API-key tab still
 *   reachable but the server rejects it once more than one account exists.
 * - smtp_configured absent (cache empty / status fetch failed) → magic-link
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
import { QUERY_KEYS } from '@/lib/query-keys';
import { LoginPage } from '@/pages/LoginPage';

const requestMagicLinkMock = vi.fn().mockResolvedValue({ sent: true });
const loginMock = vi.fn().mockResolvedValue(false);

vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  requestMagicLink: (email: string) => requestMagicLinkMock(email),
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

/** Render LoginPage with an optional pre-seeded first-run status in the cache. */
function renderLoginPage(firstRunData?: object | null) {
  const qc = makeQC();
  if (firstRunData !== undefined && firstRunData !== null) {
    qc.setQueryData(QUERY_KEYS.setup.firstRun(), firstRunData);
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
    storeLastError = null;
    loginMock.mockResolvedValue(false);
  });

  // ---------------------------------------------------------------------------
  // Default tab conditioning on smtp_configured
  // ---------------------------------------------------------------------------

  it('renders the magic-link email input by default when smtp_configured=true', () => {
    // Pre-seed cache: SMTP configured → magic-link is primary.
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'single' });
    const input = screen.getByPlaceholderText(/you@example\.com/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'email');
  });

  it('defaults to the API-key tab in single mode when smtp_configured=false', async () => {
    // Pre-seed cache: SMTP not configured, single mode → API-key should be primary.
    renderLoginPage({ configured: true, smtp_configured: false, setup_mode: 'single' });
    // useEffect fires after render to flip mode.
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i)).toBeInTheDocument();
    });
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
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
      expect(screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i)).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: /use magic link instead/i }));
    await waitFor(() => {
      expect(screen.getByRole('status')).toBeInTheDocument();
    });
    expect(screen.getByRole('status')).toHaveTextContent(/email is not configured/i);
  });

  it('shows multi-user SMTP notice without "use API key" suggestion when smtp_configured=false and multi mode', () => {
    // In multi mode the magic-link tab is primary; notice must NOT suggest API-key
    // as a working path because the backend rejects it once >1 account exists.
    renderLoginPage({ configured: true, smtp_configured: false, setup_mode: 'multi' });
    expect(screen.getByRole('status')).toHaveTextContent(/ask your admin/i);
    expect(screen.getByRole('status')).not.toHaveTextContent(/use the api key tab/i);
  });

  it('defaults to magic-link when cache is empty (status fetch failed / no data)', () => {
    // No pre-seeded cache → getQueryData returns undefined → safe fallback.
    renderLoginPage();
    expect(screen.getByPlaceholderText(/you@example\.com/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Magic-link submission
  // ---------------------------------------------------------------------------

  it('submits the email to requestMagicLink and shows confirmation', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'single' });
    const input = screen.getByPlaceholderText(/you@example\.com/i);
    await user.type(input, 'ferhat@example.com');
    await user.click(screen.getByRole('button', { name: /send magic link/i }));

    await waitFor(() => {
      expect(requestMagicLinkMock).toHaveBeenCalledWith('ferhat@example.com');
    });
    expect(await screen.findByText(/Check your email/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Toggle behaviour
  // ---------------------------------------------------------------------------

  it('toggles to the API-key form on click', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'single' });

    await user.click(screen.getByRole('button', { name: /use api key instead/i }));
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
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
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
    await user.type(input, 'some-api-key-value');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByText(/use magic-link/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Accessibility / misc
  // ---------------------------------------------------------------------------

  it('API-key input opts out of browser credential storage', async () => {
    const user = userEvent.setup();
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'single' });
    await user.click(screen.getByRole('button', { name: /use api key instead/i }));
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
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
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'single' });
    await userEvent.type(screen.getByLabelText('Email'), 'not-an-email@x');
    await userEvent.click(screen.getByRole('button', { name: /send magic link/i }));
    expect(await screen.findByText(/valid email/i)).toBeInTheDocument();
    expect(screen.queryByText(/check your connection/i)).not.toBeInTheDocument();
  });

  it('renders a <main> landmark', () => {
    renderLoginPage();
    expect(screen.getByRole('main')).toBeInTheDocument();
  });

  it('announces email-required error via role="alert"', async () => {
    renderLoginPage({ configured: true, smtp_configured: true, setup_mode: 'single' });
    await userEvent.click(screen.getByRole('button', { name: /send magic link/i }));
    expect(screen.getByRole('alert')).toHaveTextContent(/email is required/i);
  });
});

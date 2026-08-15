import { beforeEach, describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  const { ApiError } = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return createApiMock({
    getSetupStatus: async () => ({
      setup_completed: true,
      models_ready: true,
      models_downloading: [],
      topics_count: 1,
      telegram_configured: false,
      telegram_paired: false,
    }),
    // The onboarding gate calls this on every render; mock as configured AND
    // setup_completed so the gate is a no-op here (test focuses on auth gating).
    getFirstRunStatus: async () => ({ configured: true, setup_completed: true }),
    fetchDashboardMetrics: async () => ({
      total_papers: 0,
      unread_papers: 0,
      pending_papers: 0,
      due_cards: 0,
      active_projects: 0,
      topic_count: 0,
      nudge_count: 0,
      onboarding_stage: 'complete',
    }),
    verifyMagicLink: async () => ({ id: 7, email: 'a@b.com', role: 'admin' }),
    // Cookie-session bootstrap probe: default to "no valid cookie" (401) so
    // unauthenticated tests deterministically land on the login page.
    fetchAccount: async () => {
      throw new ApiError(401, JSON.stringify({ detail: 'Not authenticated' }));
    },
  });
});

// Mock fetch for auth store
vi.stubGlobal('fetch', vi.fn());

const api = await import('@/lib/api');
const { App } = await import('@/App');
const { useAuthStore } = await import('@/stores/auth-store');
const { resetAuthState } = await import('@/__tests__/auth-test-utils');

function renderApp(initialEntries: string[] = ['/']) {
  const queryClient = createTestQueryClient({
    // retryDelay: 0 keeps the bootstrap query's own retry policy (see App.tsx)
    // from adding real exponential-backoff wait time to these tests.
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  return renderWithProviders(
    <MemoryRouter initialEntries={initialEntries}>
      <App />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('App', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchAccount).mockRejectedValue(
      new api.ApiError(401, JSON.stringify({ detail: 'Not authenticated' })),
    );
    resetAuthState();
  });

  it('shows login page when not authenticated', async () => {
    useAuthStore.setState({ isAuthenticated: false, authTime: null });
    renderApp();
    // FirstRunGate shows a loading placeholder until /api/setup/status
    // resolves; once it does (configured=true mock), the LoginPage renders.
    expect(await screen.findByText('JARVIS RD Assistant')).toBeInTheDocument();
    // Default mode is magic-link (email field). API-key form
    // is reachable behind the "Use API key instead" toggle.
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });

  it('renders home for authenticated user', async () => {
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), user: { id: 1, email: 'admin.com', role: 'admin' } });
    renderApp();
    // "Dashboard" appears in both TopBar and HomePage heading. The SetupGate
    // renders a loading placeholder until the setup-status query resolves.
    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);
  });

  it('offers Papers and Discover links on an unknown route', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      user: { id: 1, email: 'admin.com', role: 'admin' },
    });
    renderApp(['/missing-page']);

    expect(await screen.findByText('Page not found')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open Papers' })).toHaveAttribute(
      'href',
      '/feed?surface=library',
    );
    expect(screen.getByRole('link', { name: 'Open Discover' })).toHaveAttribute(
      'href',
      '/feed?surface=search',
    );
  });


  it('redirects authenticated magic-link visits home without reusing the token', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      user: { id: 7, email: 'a@b.com', role: 'admin' },
    });

    renderApp(['/auth/verify#token=already-consumed-token']);

    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);
    expect(vi.mocked(api.verifyMagicLink)).not.toHaveBeenCalled();
  });


  it('keeps magic-link verification mounted while auth state flips', async () => {
    resetAuthState();
    renderApp(['/auth/verify#token=route-flip-token']);

    expect(await screen.findByText('Dashboard')).toBeInTheDocument();
    expect(vi.mocked(api.verifyMagicLink)).toHaveBeenCalledWith('route-flip-token');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  it('does not run account bootstrap on a magic-link landing', async () => {
    vi.mocked(api.fetchAccount).mockRejectedValue(
      new api.ApiError(403, JSON.stringify({ detail: 'Invalid or missing API key' })),
    );

    renderApp(['/auth/verify#token=pre-login-token']);

    expect(await screen.findByText('Dashboard')).toBeInTheDocument();
    expect(vi.mocked(api.verifyMagicLink)).toHaveBeenCalledWith('pre-login-token');
    expect(vi.mocked(api.fetchAccount)).not.toHaveBeenCalled();
  });

  it('hydrates a valid session cookie via /api/account without flashing the login page', async () => {
    // New tab: sessionStorage empty (store unauthenticated) but the HttpOnly
    // cookie is still valid — /api/account resolves the identity.
    type Account = Awaited<ReturnType<typeof api.fetchAccount>>;
    let resolveAccount!: (v: Account) => void;
    vi.mocked(api.fetchAccount).mockImplementationOnce(
      () =>
        new Promise<Account>((resolve) => {
          resolveAccount = resolve;
        }),
    );
    renderApp();

    // While the bootstrap probe is pending: loading, NOT the login form.
    expect(await screen.findByText('Loading...')).toBeInTheDocument();
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();

    resolveAccount({
      id: 7,
      email: 'a@b.com',
      role: 'admin',
      display_name: null,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    });

    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().user).toEqual({ id: 7, email: 'a@b.com', role: 'admin' });
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
  });

  it('shows an error state, not the login page, when the session probe fails with a server error', async () => {
    resetAuthState();
    const serverError = () =>
      new api.ApiError(500, JSON.stringify({ detail: 'Internal Server Error' }));
    // 1 initial attempt + 2 retries (the bootstrap query's own retry policy).
    vi.mocked(api.fetchAccount)
      .mockRejectedValueOnce(serverError())
      .mockRejectedValueOnce(serverError())
      .mockRejectedValueOnce(serverError());

    renderApp();

    expect(
      await screen.findByText(/Couldn't reach the server to check your session/),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
  });

  it('shows the login page, not an error state, when the session probe returns 401', async () => {
    resetAuthState();
    vi.mocked(api.fetchAccount).mockRejectedValueOnce(
      new api.ApiError(401, JSON.stringify({ detail: 'Not authenticated' })),
    );

    renderApp();

    expect(await screen.findByText('JARVIS RD Assistant')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });
});

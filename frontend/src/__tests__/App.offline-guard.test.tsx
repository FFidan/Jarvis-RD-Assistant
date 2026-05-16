/**
 * P1c — offline last-known-good route-guard tests.
 *
 * Canonical contract: docs/superpowers/specs/2026-05-15-shell-sidebar-admin-ia-redesign-design.md
 * "Offline / PWA contract — CANONICAL" §4 (last-known-good read mode).
 *
 * Security invariant being tested:
 *   (a) ONLINE + expired session → still redirects to /login (no regression).
 *   (b) OFFLINE + prior authenticated session → renders app shell (no /login bounce, no state clear).
 *   (c) OFFLINE + never authenticated → still gated (no new access granted).
 *   (d) ONLINE + valid session → renders app shell (unmodified happy path).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

// ------ Mock API calls so FirstRunGate + SetupGate are no-ops ------
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getSetupStatus: vi.fn().mockResolvedValue({
      setup_completed: true,
      models_ready: true,
      models_downloading: [],
      topics_count: 1,
      telegram_configured: false,
      telegram_paired: false,
    }),
    getFirstRunStatus: vi.fn().mockResolvedValue({ configured: true }),
    fetchDashboardMetrics: vi.fn().mockResolvedValue({
      total_papers: 0,
      unread_papers: 0,
      pending_papers: 0,
      due_cards: 0,
      active_projects: 0,
      topic_count: 0,
      nudge_count: 0,
      onboarding_stage: 'complete',
    }),
  };
});

// Stub fetch so the login form doesn't fire real requests.
vi.stubGlobal('fetch', vi.fn());

// ------ Mock query-persister to avoid IDB in jsdom ------
vi.mock('@/lib/query-persister', () => ({
  attachQueryPersister: vi.fn().mockReturnValue(() => {}),
  clearPersistedQueryCache: vi.fn().mockResolvedValue(undefined),
  GC_TIME: 7 * 24 * 60 * 60 * 1000,
  shouldDehydrateQuery: vi.fn().mockReturnValue(false),
  getPersistedCacheTimestamp: vi.fn().mockResolvedValue(null),
}));

// ------ Mock query-client to avoid the self-attaching persister ------
vi.mock('@/lib/query-client', async () => {
  const { QueryClient } = await import('@tanstack/react-query');
  return { queryClient: new QueryClient() };
});

const { App } = await import('@/App');
const { useAuthStore } = await import('@/stores/auth-store');

const SESSION_8H = 8 * 60 * 60 * 1000;

function renderApp(initialEntries: string[] = ['/']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={initialEntries}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', {
    configurable: true,
    value,
  });
  // Dispatch the event so useOnlineStatus() re-syncs its state.
  window.dispatchEvent(new Event(value ? 'online' : 'offline'));
}

describe('App offline route-guard (P1c)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,
      user: null,
      lastError: null,
    });
    // Default: online
    setOnline(true);
  });

  afterEach(() => {
    // Restore to online so other test suites aren't affected.
    setOnline(true);
  });

  // -------------------------------------------------------------------------
  // (a) ONLINE + expired session → still redirects to /login (no regression)
  // -------------------------------------------------------------------------
  it('(a) ONLINE + expired session: redirects to /login and clears state', async () => {
    const nineHoursAgo = Date.now() - 9 * SESSION_8H;
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: nineHoursAgo,
      apiKey: null,
      user: { id: 1, email: 'a@b.com', role: 'user' },
    });
    setOnline(true);

    renderApp(['/']);

    // The login page should render (FirstRunGate resolves to configured=true, so LoginPage shows).
    expect(await screen.findByText('JARVIS RD Assistant')).toBeInTheDocument();
    // The Email field is the magic-link login form.
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    // Auth state must be cleared by checkSession().
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  // -------------------------------------------------------------------------
  // (b) OFFLINE + prior authenticated session → renders app shell (no bounce)
  // -------------------------------------------------------------------------
  it('(b) OFFLINE + prior authenticated session: renders app shell without /login bounce', async () => {
    // Set up a session that has expired (would normally bounce to /login when ONLINE).
    const nineHoursAgo = Date.now() - 9 * SESSION_8H;
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: nineHoursAgo,
      apiKey: null,
      user: { id: 1, email: 'researcher@uni.edu', role: 'user' },
    });
    setOnline(false);

    renderApp(['/']);

    // App shell should render (Dashboard is in HomePage).
    // When offline, the guard uses hasKnownIdentity=true, skipping checkSession(),
    // so the shell renders with cached surfaces.
    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);

    // Auth state must NOT have been cleared (last-known-good preserved).
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().user).not.toBeNull();
  });

  // -------------------------------------------------------------------------
  // (c) OFFLINE + never authenticated → still gated (no new access)
  // -------------------------------------------------------------------------
  it('(c) OFFLINE + never authenticated: still shows login page (no new access)', async () => {
    useAuthStore.setState({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,
      user: null,
    });
    setOnline(false);

    renderApp(['/']);

    // Must gate to login — offline does NOT grant access to an unauthenticated user.
    expect(await screen.findByText('JARVIS RD Assistant')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // (d) ONLINE + valid session → renders app shell (unmodified happy path)
  // -------------------------------------------------------------------------
  it('(d) ONLINE + valid session: renders app shell (unchanged happy path)', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: null,
      user: { id: 2, email: 'user@example.com', role: 'user' },
    });
    setOnline(true);

    renderApp(['/']);

    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);
  });
});

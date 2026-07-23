/**
 * App.tsx unified onboarding-gate behaviour (Task A2 — wizard consolidation).
 *
 * The single gate keys on the PRE-AUTH /api/setup/status (getFirstRunStatus).
 * It shows the unified OnboardingWizard when:
 *   !setup_completed && (!configured || authed)
 * On a status error it fails OPEN for authed users (the app renders) and shows
 * an explicit error + Retry state for unauthed users (never Login/wizard).
 *
 * Asserted here:
 *   (a) fresh install (configured=false, setup_completed=false), unauthed →
 *       wizard renders (admin-create step establishes the session).
 *   (b) admin exists (configured=true, setup_completed=false) but unauthed →
 *       wizard does NOT render; the LOGIN page shows (post-auth steps need a
 *       session). After login, authed flips true and the wizard resumes.
 *   (c) admin exists, authed, setup_completed=false → wizard resumes.
 *   (d) setup_completed=true → normal app, no wizard.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

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
    // Default: fresh, unconfigured install — gate should render the wizard.
    getFirstRunStatus: vi.fn().mockResolvedValue({ configured: false, setup_completed: false }),
    runFirstRunSystemCheck: vi.fn().mockResolvedValue({
      services: [{ name: 'postgres', ok: true, detail: null }],
      all_ok: true,
    }),
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
    // Cookie-session bootstrap probe: default to "no valid cookie" (401) so
    // unauthenticated tests deterministically stay unauthenticated.
    fetchAccount: vi.fn().mockRejectedValue(
      new actual.ApiError(401, JSON.stringify({ detail: 'Not authenticated' })),
    ),
  };
});

vi.stubGlobal('fetch', vi.fn());

const api = await import('@/lib/api');
const { App } = await import('@/App');
const { useAuthStore } = await import('@/stores/auth-store');

function renderApp(initialEntries: string[] = ['/']) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('App onboarding gate (single signal)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({ configured: false, setup_completed: false });
  });

  it('(a) fresh install (unconfigured, not completed): renders the wizard for an unauthed user', async () => {
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
    renderApp(['/']);
    // The wizard's step-1 title proves the gate rendered the wizard.
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
  });

  it('(b) admin exists but not completed + unauthed: shows LOGIN (not the wizard)', async () => {
    // Post-auth steps need a session, so an unauthed user with a configured
    // install must log in first — the wizard resumes after login.
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({ configured: true, setup_completed: false });
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
    renderApp(['/']);
    expect(await screen.findByText('JARVIS RD Assistant')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.queryByText('Welcome to JARVIS')).not.toBeInTheDocument();
  });

  it('(c) admin exists, authed, not completed: resumes the wizard', async () => {
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({ configured: true, setup_completed: false });
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'k',
      user: { id: 1, email: 'a@b.com', role: 'admin' },
    });
    renderApp(['/']);
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
  });

  it('(d) setup_completed=true: renders the normal app, no wizard', async () => {
    vi.mocked(api.getFirstRunStatus).mockResolvedValue({ configured: true, setup_completed: true });
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'k',
      user: { id: 1, email: 'a@b.com', role: 'admin' },
    });
    renderApp(['/']);
    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('Welcome to JARVIS')).not.toBeInTheDocument();
  });

  // GAP-1 (unauthed sub-case): getFirstRunStatus rejects → explicit error + Retry.
  // Login would be a dead end (its submit needs the backend) and the wizard
  // could clobber an existing install — never masquerade a failed status probe
  // as either.
  it('(GAP-1) getFirstRunStatus rejects: unauthed user sees the error state with Retry (no Login, no wizard)', async () => {
    vi.mocked(api.getFirstRunStatus).mockRejectedValue(new Error('Network error'));
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
    renderApp(['/']);
    expect(
      await screen.findByText(/Couldn't reach the server to check setup status/),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    // Neither the Login form nor the wizard renders.
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
    expect(screen.queryByText('Welcome to JARVIS')).not.toBeInTheDocument();
  });

  it('(GAP-1) Retry re-probes the status and recovers to the login page', async () => {
    vi.mocked(api.getFirstRunStatus)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce({ configured: true, setup_completed: true });
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
    renderApp(['/']);
    const retry = await screen.findByRole('button', { name: 'Retry' });
    fireEvent.click(retry);
    expect(await screen.findByText('JARVIS RD Assistant')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });

  // GAP-1 (authed sub-case): getFirstRunStatus rejects → authed user sees the app (not the wizard).
  it('(GAP-1) getFirstRunStatus rejects: authed user sees the app (fail-open)', async () => {
    vi.mocked(api.getFirstRunStatus).mockRejectedValue(new Error('Network error'));
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'k',
      user: { id: 1, email: 'a@b.com', role: 'admin' },
    });
    renderApp(['/']);
    // Fail-open: authed user must reach the app, not the wizard.
    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('Welcome to JARVIS')).not.toBeInTheDocument();
  });
});

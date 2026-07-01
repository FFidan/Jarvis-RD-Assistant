import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
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
    // The onboarding gate calls this on every render; mock as configured AND
    // setup_completed so the gate is a no-op here (test focuses on auth gating).
    getFirstRunStatus: vi.fn().mockResolvedValue({ configured: true, setup_completed: true }),
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
    verifyMagicLink: vi.fn().mockResolvedValue({ id: 7, email: 'a@b.com', role: 'admin' }),
  };
});

// Mock fetch for auth store
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

describe('App', () => {
  it('shows login page when not authenticated', async () => {
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null });
    renderApp();
    // FirstRunGate shows a loading placeholder until /api/setup/status
    // resolves; once it does (configured=true mock), the LoginPage renders.
    expect(await screen.findByText('JARVIS RD Assistant')).toBeInTheDocument();
    // Default mode is magic-link (email field). API-key form
    // is reachable behind the "Use API key instead" toggle.
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });

  it('renders home for authenticated user', async () => {
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: 'test-key' });
    renderApp();
    // "Dashboard" appears in both TopBar and HomePage heading. The SetupGate
    // renders a loading placeholder until the setup-status query resolves.
    const dashboards = await screen.findAllByText('Dashboard');
    expect(dashboards.length).toBeGreaterThanOrEqual(1);
  });


  it('keeps magic-link verification mounted while auth state flips', async () => {
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
    renderApp(['/auth/verify?token=route-flip-token']);

    expect(await screen.findByText('Dashboard')).toBeInTheDocument();
    expect(vi.mocked(api.verifyMagicLink)).toHaveBeenCalledWith('route-flip-token');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });
});

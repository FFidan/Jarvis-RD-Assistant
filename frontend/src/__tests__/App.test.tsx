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
    // FirstRunGate calls this on every render; mock as configured so
    // the gate is a no-op in this test (test focuses on auth/setup gating only).
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

// Mock fetch for auth store
vi.stubGlobal('fetch', vi.fn());

const { App } = await import('@/App');
const { useAuthStore } = await import('@/stores/auth-store');

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
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
});

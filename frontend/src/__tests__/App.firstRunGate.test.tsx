/**
 * App.tsx FirstRunGate redirect behaviour.
 *
 * Asserts that when /api/setup/status reports configured=false, ALL routes
 * (auth and post-auth) are redirected to /first-run, and the wizard renders.
 */
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
    // Unconfigured install — gate should redirect us to /first-run.
    getFirstRunStatus: vi.fn().mockResolvedValue({ configured: false }),
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
  };
});

vi.stubGlobal('fetch', vi.fn());

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

describe('App FirstRunGate', () => {
  it('redirects unauthenticated users to /first-run when install is unconfigured', async () => {
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
    renderApp(['/']);
    // The wizard's step-1 title proves the redirect happened AND the wizard rendered.
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
  });

  it('redirects authenticated users to /first-run when install is unconfigured', async () => {
    // Edge case: a stale session lingers but the install was wiped — still
    // bounce them to the wizard rather than crashing on missing data.
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'stale',
      user: null,
    });
    renderApp(['/cards']);
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
  });
});

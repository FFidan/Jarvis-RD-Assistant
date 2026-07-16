/**
 * Suspense wrapping for lazy-loaded routes.
 *
 * Verifies that the lazy routes (OnboardingWizard via the onboarding gate,
 * LogsPage, AdminUsersPage, PaperDetailPage, ResearchFeedPage) show the
 * <PageFallback /> ("Loading...") while their lazy module is pending, and
 * that the ErrorBoundary fallback ("Something went wrong") is absent.
 *
 * Strategy: replace each lazy page module with a component that throws a
 * never-resolving Promise (the React Suspense contract).  Without a <Suspense>
 * boundary the thrown promise bubbles up to <ErrorBoundary> and React would
 * render "Something went wrong".  With the fix, the nearest <Suspense>
 * boundary catches the pending state and renders <PageFallback />.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

// ---------------------------------------------------------------------------
// A React component that always suspends (never resolves).
// ---------------------------------------------------------------------------
const neverResolve = new Promise<never>(() => { /* intentionally never resolves */ });
function SuspendForever(): never {
  throw neverResolve;
}

// ---------------------------------------------------------------------------
// Mock lazy page modules so they stay suspended during the test.
// Each mock must match the named-export shape used in the lazy() call in App.tsx.
// ---------------------------------------------------------------------------
vi.mock('@/pages/OnboardingWizard', () => ({ OnboardingWizard: SuspendForever }));
vi.mock('@/pages/LogsPage', () => ({ LogsPage: SuspendForever }));
vi.mock('@/pages/AdminUsersPage', () => ({ AdminUsersPage: SuspendForever }));
vi.mock('@/pages/PaperDetailPage', () => ({ PaperDetailPage: SuspendForever }));
vi.mock('@/pages/ResearchFeedPage', () => ({ ResearchFeedPage: SuspendForever }));

// ---------------------------------------------------------------------------
// Standard API mocks so gate components don't block rendering.
// ---------------------------------------------------------------------------
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
    getFirstRunStatus: vi.fn().mockResolvedValue({ configured: true, setup_completed: true }),
    fetchDashboardMetrics: vi.fn().mockResolvedValue({
      total_papers: 0, unread_papers: 0, pending_papers: 0,
      due_cards: 0, active_projects: 0, topic_count: 0,
      nudge_count: 0, onboarding_stage: 'complete',
    }),
    runFirstRunSystemCheck: vi.fn().mockResolvedValue({ services: [], all_ok: true }),
    // Cookie-session bootstrap probe: default to "no valid cookie" (401) so
    // unauthenticated tests deterministically stay unauthenticated.
    fetchAccount: vi.fn().mockRejectedValue(new Error('401 Unauthorized')),
  };
});

vi.stubGlobal('fetch', vi.fn());

const { App } = await import('@/App');
const { useAuthStore } = await import('@/stores/auth-store');

function renderApp(initialEntries: string[]) {
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

describe('lazy routes wrapped in <Suspense>', () => {
  it('onboarding gate (pre-auth, unconfigured) shows PageFallback while OnboardingWizard suspends', async () => {
    // Fresh install: unconfigured + not setup_completed → the gate renders the
    // lazy OnboardingWizard, which suspends. "Loading..." must show (gate
    // spinner or Suspense fallback) and the ErrorBoundary must be absent.
    vi.mocked((await import('@/lib/api')).getFirstRunStatus).mockResolvedValue({
      configured: false,
      setup_completed: false,
    });
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null });
    renderApp(['/']);
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });

  it('onboarding gate (authed, configured but not completed) shows PageFallback while OnboardingWizard suspends', async () => {
    // Resume case: admin exists, authed, but setup not completed → gate renders
    // the wizard to resume the post-auth steps; it suspends.
    vi.mocked((await import('@/lib/api')).getFirstRunStatus).mockResolvedValue({
      configured: true,
      setup_completed: false,
    });
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: 'k' });
    renderApp(['/']);
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });

  it('/logs shows PageFallback while LogsPage suspends', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'k',
      user: { id: 1, email: 'a@b.com', role: 'admin' },
    });
    renderApp(['/logs']);
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });

  it('/admin/users shows PageFallback while AdminUsersPage suspends', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      apiKey: 'k',
      user: { id: 1, email: 'a@b.com', role: 'admin' },
    });
    renderApp(['/admin/users']);
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });

  it('/paper/:paperId shows PageFallback while PaperDetailPage suspends', () => {
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: 'k' });
    renderApp(['/paper/123']);
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });

  // ResearchFeedPage is now lazy-loaded
  it('/feed shows PageFallback while ResearchFeedPage suspends', () => {
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: 'k' });
    renderApp(['/feed']);
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });
});

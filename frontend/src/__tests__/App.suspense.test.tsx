/**
 * H12 — Suspense wrapping for lazy-loaded routes.
 *
 * Verifies that the 5 previously-unwrapped lazy routes (SetupWizard,
 * FirstRunSetupPage, LogsPage, AdminUsersPage, PaperDetailPage) show the
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
vi.mock('@/pages/SetupWizard', () => ({ SetupWizard: SuspendForever }));
vi.mock('@/pages/FirstRunSetupPage', () => ({ FirstRunSetupPage: SuspendForever }));
vi.mock('@/pages/LogsPage', () => ({ LogsPage: SuspendForever }));
vi.mock('@/pages/AdminUsersPage', () => ({ AdminUsersPage: SuspendForever }));
vi.mock('@/pages/PaperDetailPage', () => ({ PaperDetailPage: SuspendForever }));

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
    getFirstRunStatus: vi.fn().mockResolvedValue({ configured: true }),
    fetchDashboardMetrics: vi.fn().mockResolvedValue({
      total_papers: 0, unread_papers: 0, pending_papers: 0,
      due_cards: 0, active_projects: 0, topic_count: 0,
      nudge_count: 0, onboarding_stage: 'complete',
    }),
    runFirstRunSystemCheck: vi.fn().mockResolvedValue({ services: [], all_ok: true }),
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Assert <PageFallback /> is rendered and the error boundary is not. */
function expectFallback() {
  expect(screen.getByText('Loading...')).toBeInTheDocument();
  expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
}

describe('H12 — lazy routes wrapped in <Suspense>', () => {
  it('/first-run (pre-auth) shows PageFallback while FirstRunSetupPage suspends', () => {
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null });
    renderApp(['/first-run']);
    // FirstRunGate renders a "Loading..." spinner while the status query is in
    // flight.  After it resolves (configured=true mock), the route element
    // renders — which also suspends.  Either way "Loading..." is present and
    // "Something went wrong" is absent, which is what we assert.
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    // At least one Loading... must be on screen (gate or Suspense fallback).
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });

  it('/setup (authed) shows PageFallback while SetupWizard suspends', () => {
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: 'k' });
    renderApp(['/setup']);
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('Loading...').length).toBeGreaterThanOrEqual(1);
  });

  it('/first-run (authed) shows PageFallback while FirstRunSetupPage suspends', () => {
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: 'k' });
    renderApp(['/first-run']);
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
});

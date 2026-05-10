import { lazy, Suspense, useEffect } from 'react';
import type { ReactNode } from 'react';
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { setNavigate } from '@/lib/navigate-bridge';
import { AppShell } from '@/components/layout/AppShell';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { RouteErrorBoundary } from '@/components/RouteErrorBoundary';
import { LoginPage } from '@/pages/LoginPage';
import { AuthVerifyPage } from '@/pages/AuthVerifyPage';
import { HomePage } from '@/pages/HomePage';
import { MyDayPage } from '@/pages/MyDayPage';
import { SettingsPage } from '@/pages/SettingsPage';
import { ProjectsPage } from '@/pages/ProjectsPage';
import { LearningCardsPage } from '@/pages/LearningCardsPage';
import { NotFoundPage } from '@/pages/NotFoundPage';
import { ExtractionTablePage } from '@/pages/ExtractionTablePage';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { PulseDeckPage } from '@/pages/PulseDeckPage';
import { getFirstRunStatus, getSetupStatus } from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import { PomodoroAutoLogger } from '@/components/layout/PomodoroAutoLogger';
import { AdminOnlyRoute } from '@/components/auth/AdminOnlyRoute';

// Heavy pages lazy-loaded to reduce initial bundle size.
// - Graph pages pull cytoscape (~432 kB).
// - Analytics page pulls recharts (~404 kB).
// - LogsPage pulls recharts (via ErrorSparkLine) — lazy here removes recharts
//   from the main bundle for users who never visit /logs.
// - PaperDetailPage pulls react-markdown + math/syntax stacks (~392 kB).
// - Setup wizards & AdminUsersPage are large (12-16 kB each) and only used
//   by admins / on first run.
const KnowledgeGraphPage = lazy(() =>
  import('@/pages/KnowledgeGraphPage').then((m) => ({ default: m.KnowledgeGraphPage })),
);
const CitationGraphPage = lazy(() =>
  import('@/pages/CitationGraphPage').then((m) => ({ default: m.CitationGraphPage })),
);
const AnalyticsPage = lazy(() =>
  import('@/pages/AnalyticsPage').then((m) => ({ default: m.AnalyticsPage })),
);
const LogsPage = lazy(() =>
  import('@/pages/LogsPage').then((m) => ({ default: m.LogsPage })),
);
const PaperDetailPage = lazy(() =>
  import('@/pages/PaperDetailPage').then((m) => ({ default: m.PaperDetailPage })),
);
const AdminUsersPage = lazy(() =>
  import('@/pages/AdminUsersPage').then((m) => ({ default: m.AdminUsersPage })),
);
const SetupWizard = lazy(() =>
  import('@/pages/SetupWizard').then((m) => ({ default: m.SetupWizard })),
);
const FirstRunSetupPage = lazy(() =>
  import('@/pages/FirstRunSetupPage').then((m) => ({ default: m.FirstRunSetupPage })),
);

function PageFallback() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center text-sm text-muted-foreground">
      Loading...
    </div>
  );
}

/**
 * Gates the main AppShell behind a setup-status check. When the server reports
 * setup_completed=false, users are redirected to /setup?step=1. Backward-compat:
 * users with setup already marked complete never see a wizard or extra network
 * hop beyond a single cached setup-status query.
 */
function SetupGate({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['setup-status'],
    queryFn: getSetupStatus,
    staleTime: 30_000,
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }

  // If the API fails (e.g. offline), fail open so users still reach the app.
  if (!isError && data && !data.setup_completed && location.pathname !== '/setup') {
    return <Navigate to="/setup?step=1" replace />;
  }

  return <>{children}</>;
}

function NavigateBridgeRegistrar() {
  const navigate = useNavigate();
  useEffect(() => { setNavigate(navigate); }, [navigate]);
  return null;
}

/**
 * WS-2F: gates ALL routes (auth + post-auth) behind a /api/setup/status check.
 * When the install reports configured=false (no admin user exists yet), every
 * route redirects to /first-run so the operator can run the bootstrap wizard.
 *
 * Once configured=true (after the wizard's create-admin step), the gate becomes
 * a no-op and normal auth/route flow resumes.
 *
 * Failure-mode: if the status query errors (backend down), we fail OPEN so the
 * login page is still reachable — losing access to your own install because
 * the status probe blipped would be hostile.
 */
function FirstRunGate({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['first-run-status'],
    queryFn: getFirstRunStatus,
    staleTime: 30_000,
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!isError && data && !data.configured && location.pathname !== '/first-run') {
    return <Navigate to="/first-run" replace />;
  }

  // Conversely, if the install IS configured but the user lands on /first-run
  // (stale link, refresh after wizard), kick them home.
  if (!isError && data?.configured && location.pathname === '/first-run') {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}

export function App() {
  const { isAuthenticated, checkSession } = useAuthStore();
  const authed = isAuthenticated && checkSession();

  if (!authed) {
    return (
      <FirstRunGate>
        <Routes>
          {/* WS-2F: pre-auth wizard — reachable even with no session, by design. */}
          <Route path="/first-run" element={<RouteErrorBoundary><FirstRunSetupPage /></RouteErrorBoundary>} />
          {/* Magic-link landing must be reachable without an existing session — */}
          {/* it's the page that CREATES the session. */}
          <Route path="/auth/verify" element={<RouteErrorBoundary><AuthVerifyPage /></RouteErrorBoundary>} />
          <Route path="*" element={<RouteErrorBoundary><LoginPage /></RouteErrorBoundary>} />
        </Routes>
      </FirstRunGate>
    );
  }

  return (
    <ErrorBoundary>
      <FirstRunGate>
      <SetupGate>
        <NavigateBridgeRegistrar />
        <PomodoroAutoLogger />
        <Routes>
          <Route path="/setup" element={<RouteErrorBoundary><SetupWizard /></RouteErrorBoundary>} />
          {/* WS-2F: when an authed-but-stale session lingers on an unconfigured */}
          {/* install, FirstRunGate redirects to /first-run; this route renders */}
          {/* the wizard outside of AppShell so the user can complete setup. */}
          <Route path="/first-run" element={<RouteErrorBoundary><FirstRunSetupPage /></RouteErrorBoundary>} />
          <Route
            path="*"
            element={
              <AppShell>
                <Routes>
                  <Route path="/" element={<RouteErrorBoundary><HomePage /></RouteErrorBoundary>} />
                  <Route path="/my-day" element={<RouteErrorBoundary><MyDayPage /></RouteErrorBoundary>} />
                  <Route path="settings" element={<RouteErrorBoundary><SettingsPage /></RouteErrorBoundary>} />
                  <Route path="analytics" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><AnalyticsPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="logs" element={<RouteErrorBoundary><AdminOnlyRoute><LogsPage /></AdminOnlyRoute></RouteErrorBoundary>} />
                  <Route path="admin/users" element={<RouteErrorBoundary><AdminOnlyRoute><AdminUsersPage /></AdminOnlyRoute></RouteErrorBoundary>} />
                  <Route path="extractions" element={<RouteErrorBoundary><ExtractionTablePage /></RouteErrorBoundary>} />
                  <Route path="projects" element={<RouteErrorBoundary><ProjectsPage /></RouteErrorBoundary>} />
                  <Route path="cards" element={<RouteErrorBoundary><LearningCardsPage /></RouteErrorBoundary>} />
                  <Route path="feed" element={<RouteErrorBoundary><ResearchFeedPage /></RouteErrorBoundary>} />
                  <Route path="ask" element={<Navigate to="/feed?surface=ask" replace />} />
                  <Route path="pulse" element={<RouteErrorBoundary><PulseDeckPage /></RouteErrorBoundary>} />
                  <Route path="paper/:paperId" element={<RouteErrorBoundary><PaperDetailPage /></RouteErrorBoundary>} />
                  <Route path="citations" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><CitationGraphPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="knowledge" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><KnowledgeGraphPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="*" element={<RouteErrorBoundary><NotFoundPage /></RouteErrorBoundary>} />
                </Routes>
              </AppShell>
            }
          />
        </Routes>
      </SetupGate>
      </FirstRunGate>
    </ErrorBoundary>
  );
}

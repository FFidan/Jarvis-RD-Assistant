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
import { PaperDetailPage } from '@/pages/PaperDetailPage';
import { PulseDeckPage } from '@/pages/PulseDeckPage';
import { LogsPage } from '@/pages/LogsPage';
import { AdminUsersPage } from '@/pages/AdminUsersPage';
import { SetupWizard } from '@/pages/SetupWizard';
import { getSetupStatus } from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import { PomodoroAutoLogger } from '@/components/layout/PomodoroAutoLogger';
import { AdminOnlyRoute } from '@/components/auth/AdminOnlyRoute';

// Heavy pages lazy-loaded to reduce initial bundle size
const KnowledgeGraphPage = lazy(() =>
  import('@/pages/KnowledgeGraphPage').then((m) => ({ default: m.KnowledgeGraphPage })),
);
const CitationGraphPage = lazy(() =>
  import('@/pages/CitationGraphPage').then((m) => ({ default: m.CitationGraphPage })),
);
const AnalyticsPage = lazy(() =>
  import('@/pages/AnalyticsPage').then((m) => ({ default: m.AnalyticsPage })),
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

export function App() {
  const { isAuthenticated, checkSession } = useAuthStore();
  const authed = isAuthenticated && checkSession();

  if (!authed) {
    return (
      <Routes>
        {/* Magic-link landing must be reachable without an existing session — */}
        {/* it's the page that CREATES the session. */}
        <Route path="/auth/verify" element={<RouteErrorBoundary><AuthVerifyPage /></RouteErrorBoundary>} />
        <Route path="*" element={<RouteErrorBoundary><LoginPage /></RouteErrorBoundary>} />
      </Routes>
    );
  }

  return (
    <ErrorBoundary>
      <SetupGate>
        <NavigateBridgeRegistrar />
        <PomodoroAutoLogger />
        <Routes>
          <Route path="/setup" element={<RouteErrorBoundary><SetupWizard /></RouteErrorBoundary>} />
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
    </ErrorBoundary>
  );
}

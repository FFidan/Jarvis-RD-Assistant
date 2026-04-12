import type { ReactNode } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AppShell } from '@/components/layout/AppShell';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { RouteErrorBoundary } from '@/components/RouteErrorBoundary';
import { LoginPage } from '@/pages/LoginPage';
import { HomePage } from '@/pages/HomePage';
import { MyDayPage } from '@/pages/MyDayPage';
import { SettingsPage } from '@/pages/SettingsPage';
import { ProjectsPage } from '@/pages/ProjectsPage';
import { LearningCardsPage } from '@/pages/LearningCardsPage';
import { NotFoundPage } from '@/pages/NotFoundPage';
import { AnalyticsPage } from '@/pages/AnalyticsPage';
import { ExtractionTablePage } from '@/pages/ExtractionTablePage';
import { ResearchFeedPage } from '@/pages/ResearchFeedPage';
import { PaperDetailPage } from '@/pages/PaperDetailPage';
import { CitationGraphPage } from '@/pages/CitationGraphPage';
import { KnowledgeGraphPage } from '@/pages/KnowledgeGraphPage';
import { SetupWizard } from '@/pages/SetupWizard';
import { getSetupStatus } from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';

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

export function App() {
  const { isAuthenticated, checkSession } = useAuthStore();
  const authed = isAuthenticated && checkSession();

  if (!authed) {
    return (
      <Routes>
        <Route path="*" element={<RouteErrorBoundary><LoginPage /></RouteErrorBoundary>} />
      </Routes>
    );
  }

  return (
    <ErrorBoundary>
      <SetupGate>
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
                  <Route path="analytics" element={<RouteErrorBoundary><AnalyticsPage /></RouteErrorBoundary>} />
                  <Route path="extractions" element={<RouteErrorBoundary><ExtractionTablePage /></RouteErrorBoundary>} />
                  <Route path="projects" element={<RouteErrorBoundary><ProjectsPage /></RouteErrorBoundary>} />
                  <Route path="cards" element={<RouteErrorBoundary><LearningCardsPage /></RouteErrorBoundary>} />
                  <Route path="feed" element={<RouteErrorBoundary><ResearchFeedPage /></RouteErrorBoundary>} />
                  <Route path="paper/:paperId" element={<RouteErrorBoundary><PaperDetailPage /></RouteErrorBoundary>} />
                  <Route path="citations" element={<RouteErrorBoundary><CitationGraphPage /></RouteErrorBoundary>} />
                  <Route path="knowledge" element={<RouteErrorBoundary><KnowledgeGraphPage /></RouteErrorBoundary>} />
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

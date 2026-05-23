import { lazy, Suspense, useEffect } from 'react';
import type { ReactNode } from 'react';
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { setNavigate } from '@/lib/navigate-bridge';
import { AppShell } from '@/components/layout/AppShell';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { RouteErrorBoundary } from '@/components/RouteErrorBoundary';
import { LoginPage } from '@/pages/LoginPage';
import { AuthVerifyPage } from '@/pages/AuthVerifyPage';
import { HomePage } from '@/pages/HomePage';
import { NotFoundPage } from '@/pages/NotFoundPage';
import { useOnlineStatus } from '@/hooks/use-online-status';
// ResearchFeedPage is lazy-loaded (DOM-F-10) — keeps ~26 kB of feed components
// out of the HomePage initial bundle.
const ResearchFeedPage = lazy(() =>
  import('@/pages/ResearchFeedPage').then((m) => ({ default: m.ResearchFeedPage })),
);
import { PulseDeckPage } from '@/pages/PulseDeckPage';
import { AskPage } from '@/pages/AskPage';
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
// - ResearchFeedPage (DOM-F-10): ~26 kB of feed components excluded from
//   the HomePage initial bundle.
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
const AdminAuditLogPage = lazy(() =>
  import('@/pages/AdminAuditLogPage').then((m) => ({ default: m.AdminAuditLogPage })),
);
const AdminSystemHealthPage = lazy(() =>
  import('@/pages/AdminSystemHealthPage').then((m) => ({ default: m.AdminSystemHealthPage })),
);
const SetupWizard = lazy(() =>
  import('@/pages/SetupWizard').then((m) => ({ default: m.SetupWizard })),
);
const FirstRunSetupPage = lazy(() =>
  import('@/pages/FirstRunSetupPage').then((m) => ({ default: m.FirstRunSetupPage })),
);
const MyDayPage = lazy(() =>
  import('@/pages/MyDayPage').then((m) => ({ default: m.MyDayPage })),
);
const SettingsPage = lazy(() =>
  import('@/pages/SettingsPage').then((m) => ({ default: m.SettingsPage })),
);
const ProjectsPage = lazy(() =>
  import('@/pages/ProjectsPage').then((m) => ({ default: m.ProjectsPage })),
);
const LearningCardsPage = lazy(() =>
  import('@/pages/LearningCardsPage').then((m) => ({ default: m.LearningCardsPage })),
);
const ExtractionTablePage = lazy(() =>
  import('@/pages/ExtractionTablePage').then((m) => ({ default: m.ExtractionTablePage })),
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
    queryKey: QUERY_KEYS.setup.status(),
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
    queryKey: QUERY_KEYS.setup.firstRun(),
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
  const { isAuthenticated, isSessionValid, expireSession } = useAuthStore();
  // Offline / PWA contract — CANONICAL (shell-sidebar-admin-ia-redesign-design.md §4):
  // When the device is offline AND a prior authenticated identity exists (isAuthenticated
  // is true with a recent authTime), do NOT hard-bounce to /login. Instead allow the
  // app shell to render so cached read-only surfaces (Library, Paper Detail) are
  // accessible in last-known-good read mode.
  //
  // SECURITY INVARIANT: when `online===true` (or unknowable), the guard is
  // byte-equivalent to the pre-offline-track implementation — same expiry logic,
  // same /login redirect. The softening is exclusively:
  //   OFFLINE + prior authenticated identity → allow app shell (read-only cache).
  //   OFFLINE + never authenticated (isAuthenticated===false) → still gated (no new access).
  //   ONLINE + expired session → still redirects/clears as before (no security regression).
  const { online } = useOnlineStatus();
  const hasKnownIdentity = isAuthenticated;
  // isSessionValid() is a pure read — safe to call during React render (no set()).
  // ONLINE path: session must exist and not be expired.
  // OFFLINE + prior identity path: skip expiry check so the last-known-good session
  //   remains in place for cached read-only surfaces.
  const sessionOk = isSessionValid();
  const authed = online ? isAuthenticated && sessionOk : hasKnownIdentity;

  // Side-effect: when online and the session has expired, clear the store AFTER
  // render (useEffect) — never during render — to avoid React 19 concurrent-mode
  // "update during render of a different component" warnings / re-render loops.
  useEffect(() => {
    if (online && isAuthenticated && !sessionOk) {
      expireSession();
    }
  }, [online, isAuthenticated, sessionOk, expireSession]);

  if (!authed) {
    return (
      <FirstRunGate>
        <Routes>
          {/* WS-2F: pre-auth wizard — reachable even with no session, by design. */}
          <Route path="/first-run" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><FirstRunSetupPage /></Suspense></RouteErrorBoundary>} />
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
          <Route path="/setup" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><SetupWizard /></Suspense></RouteErrorBoundary>} />
          {/* WS-2F: when an authed-but-stale session lingers on an unconfigured */}
          {/* install, FirstRunGate redirects to /first-run; this route renders */}
          {/* the wizard outside of AppShell so the user can complete setup. */}
          <Route path="/first-run" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><FirstRunSetupPage /></Suspense></RouteErrorBoundary>} />
          <Route
            path="*"
            element={
              <AppShell>
                <Routes>
                  <Route path="/" element={<RouteErrorBoundary><HomePage /></RouteErrorBoundary>} />
                  <Route path="/my-day" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><MyDayPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="settings" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><SettingsPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="analytics" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><AnalyticsPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="logs" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><AdminOnlyRoute><LogsPage /></AdminOnlyRoute></Suspense></RouteErrorBoundary>} />
                  <Route path="admin/users" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><AdminOnlyRoute><AdminUsersPage /></AdminOnlyRoute></Suspense></RouteErrorBoundary>} />
                  <Route path="admin/audit-log" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><AdminOnlyRoute><AdminAuditLogPage /></AdminOnlyRoute></Suspense></RouteErrorBoundary>} />
                  <Route path="admin/system-health" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><AdminOnlyRoute><AdminSystemHealthPage /></AdminOnlyRoute></Suspense></RouteErrorBoundary>} />
                  <Route path="extractions" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><ExtractionTablePage /></Suspense></RouteErrorBoundary>} />
                  <Route path="projects" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><ProjectsPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="cards" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><LearningCardsPage /></Suspense></RouteErrorBoundary>} />
                  <Route path="feed" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><ResearchFeedPage /></Suspense></RouteErrorBoundary>} />
                  {/* Feed spec §3.4 / Shell spec group Ⅳ: Ask is its own */}
                  {/* nav destination, NOT a folded-in feed filter. The old */}
                  {/* <Navigate to="/feed?surface=ask"> redirect is removed. */}
                  <Route path="ask" element={<RouteErrorBoundary><AskPage /></RouteErrorBoundary>} />
                  <Route path="pulse" element={<RouteErrorBoundary><PulseDeckPage /></RouteErrorBoundary>} />
                  <Route path="paper/:paperId" element={<RouteErrorBoundary><Suspense fallback={<PageFallback />}><PaperDetailPage /></Suspense></RouteErrorBoundary>} />
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

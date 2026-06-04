import { lazy, Suspense, useEffect } from 'react';
import { Routes, Route, Navigate, useNavigate } from 'react-router-dom';
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
import { getFirstRunStatus } from '@/lib/api';
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
const OnboardingWizard = lazy(() =>
  import('@/pages/OnboardingWizard').then((m) => ({ default: m.OnboardingWizard })),
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

function NavigateBridgeRegistrar() {
  const navigate = useNavigate();
  useEffect(() => { setNavigate(navigate); }, [navigate]);
  return null;
}

export function App() {
  const { isAuthenticated, isSessionValid, expireSession } = useAuthStore();

  // Single onboarding gate (Task A2 — wizard consolidation). Keyed on the
  // PRE-AUTH /api/setup/status (reachable with no session, HTTP 200) so the
  // same query drives the wizard across the mid-flow auth boundary. Replaces
  // the former FirstRunGate + SetupGate pair.
  const {
    data: firstRun,
    isLoading: firstRunLoading,
    isError: firstRunError,
  } = useQuery({
    queryKey: QUERY_KEYS.setup.firstRun(),
    queryFn: getFirstRunStatus,
    staleTime: 30_000,
    retry: false,
  });
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

  // Keep the loading spinner while the gate's status query is in flight, so we
  // don't flash the login page then bounce into the wizard.
  if (firstRunLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }

  // Show the unified wizard when setup is incomplete. The `(!configured ||
  // authed)` clause spans the mid-flow auth boundary: a fresh install (no admin
  // yet) shows the wizard so its admin-create step can establish the session;
  // but if an admin already exists and the user isn't authed, fall through to
  // LOGIN first (the post-auth steps need a session) — after login, `authed`
  // flips true and the wizard shows to RESUME. Fails OPEN on a status blip so a
  // transient error never locks the operator out.
  const showOnboarding =
    !firstRunError && !!firstRun && !firstRun.setup_completed && (!firstRun.configured || authed);

  if (showOnboarding) {
    return (
      <RouteErrorBoundary>
        <Suspense fallback={<PageFallback />}>
          <OnboardingWizard firstRun={firstRun} authed={authed} />
        </Suspense>
      </RouteErrorBoundary>
    );
  }

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
        <NavigateBridgeRegistrar />
        <PomodoroAutoLogger />
        <Routes>
          {/* Setup is complete (the gate above handles the incomplete case).
              Resolve old deep links so stale /setup and /first-run URLs land
              home instead of NotFound. */}
          <Route path="/setup" element={<Navigate to="/" replace />} />
          <Route path="/first-run" element={<Navigate to="/" replace />} />
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
    </ErrorBoundary>
  );
}

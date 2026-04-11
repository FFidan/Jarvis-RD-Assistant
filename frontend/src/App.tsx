import { Routes, Route } from 'react-router-dom';
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
import { useAuthStore } from '@/stores/auth-store';

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
    </ErrorBoundary>
  );
}

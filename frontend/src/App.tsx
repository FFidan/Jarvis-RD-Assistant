import { Routes, Route } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { ErrorBoundary } from '@/components/ErrorBoundary';
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
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }

  return (
    <ErrorBoundary>
      <AppShell>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/my-day" element={<MyDayPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="analytics" element={<AnalyticsPage />} />
          <Route path="extractions" element={<ExtractionTablePage />} />
          <Route path="projects" element={<ProjectsPage />} />
          <Route path="cards" element={<LearningCardsPage />} />
          <Route path="feed" element={<ResearchFeedPage />} />
          <Route path="paper/:paperId" element={<PaperDetailPage />} />
          <Route path="citations" element={<CitationGraphPage />} />
          <Route path="knowledge" element={<KnowledgeGraphPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </AppShell>
    </ErrorBoundary>
  );
}

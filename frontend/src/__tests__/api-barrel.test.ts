/**
 * Barrel export-parity guard for the `@/lib/api` domain split (Task B2-T5).
 *
 * `src/lib/api.ts` (231 exports) was mechanically split into domain submodules
 * under `src/lib/api/` behind a barrel `index.ts`. This test pins that the
 * barrel still re-exports the key public surface so the ~91 test mocks of
 * `@/lib/api` and every app import keep resolving unchanged.
 */
import { describe, it, expect } from 'vitest';
import * as api from '@/lib/api';

describe('@/lib/api barrel export parity', () => {
  // A representative slice of the public surface, one per domain module, plus
  // the shared core primitives. If any goes missing the split regressed.
  const expectedValues = [
    // core
    'apiFetch',
    'apiFetchRaw',
    'ApiError',
    'checkHealth',
    'fetchStackHealth',
    // auth
    'requestMagicLink',
    'verifyMagicLink',
    'logoutSession',
    'listUsers',
    'listAuditLog',
    // system
    'getSystemReadiness',
    'fetchSystemModels',
    'fetchDashboardMetrics',
    'getSystemCapabilities',
    'getAISettings',
    // settings
    'fetchTopics',
    'fetchSources',
    'fetchConfig',
    'setConfig',
    'getProviderStatuses',
    'saveSetupMode',
    'downloadMyData',
    // analytics
    'fetchAnalyticsSummary',
    'fetchContradictions',
    'scanContradictions',
    'scanPaperContradictions',
    // projects
    'fetchProjects',
    'createTask',
    'fetchMilestones',
    'linkPaper',
    'searchLibrary',
    // cards
    'fetchDecks',
    'fetchCards',
    'getNextReview',
    'submitReview',
    'exportAnki',
    'generateCardsJob',
    // papers
    'fetchFeed',
    'fetchFeedPapers',
    'fetchFeedCounts',
    'fetchFeedCountsWithFacets',
    'searchPreview',
    'savePaper',
    'starPaper',
    'submitFeedback',
    'fetchRecommendationFeedback',
    'getKnowledgeGraph',
    'getCitationGraph',
    'fetchSnapshot',
    'downloadExtractionCsv',
    'promoteZoteroNote',
    // pulse
    'fetchPulseToday',
    'ratePulseCard',
    'generatePulseNow',
    'getPulseSourceHealth',
    // jobs
    'createJob',
    'getJob',
    'listJobs',
    'cancelJob',
    // zotero
    'zoteroTest',
    'zoteroPushPaper',
    'zoteroResync',
    // myday
    'fetchMyDay',
    'getMyDayBundle',
    'getJournalEntry',
    'upsertJournalEntry',
    'fetchYesterday',
    'fetchThreads',
    'fetchAccount',
    'fetchWeeklyDigest',
  ] as const;

  it('re-exports every key public function/class from the barrel', () => {
    const missing = expectedValues.filter(
      (name) => typeof (api as Record<string, unknown>)[name] !== 'function',
    );
    expect(missing).toEqual([]);
  });

  it('exposes ApiError as a constructable class', () => {
    const err = new api.ApiError(404, 'Not found');
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(404);
    expect(err.name).toBe('ApiError');
  });

  it('does NOT leak the internal-only helpers onto the public surface', () => {
    // These were un-exported from the original api.ts; the barrel keeps them private.
    for (const internal of [
      'authHeaders',
      'handleAuthFailure',
      '_sessionExpiredToastShownAt',
      'triggerBlobDownload',
    ]) {
      expect((api as Record<string, unknown>)[internal]).toBeUndefined();
    }
  });
});

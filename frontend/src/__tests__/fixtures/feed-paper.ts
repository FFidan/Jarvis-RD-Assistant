/** Shared FeedPaper test-data factory. */
import type { FeedPaper } from '@/types';

/**
 * Build a complete inbox-state FeedPaper; pass `overrides` for the fields a
 * test cares about. Safe to reference from inside a `vi.mock` factory via
 * dynamic import:
 *
 *   const { makeFeedPaper } = await import('@/__tests__/fixtures/feed-paper');
 */
export function makeFeedPaper(overrides: Partial<FeedPaper> = {}): FeedPaper {
  return {
    id: 1,
    external_id: 'ext-001',
    source_type: 'arxiv',
    title: 'Action Item Paper',
    authors: ['Author A'],
    abstract: null,
    published_date: '2026-01-01',
    url: 'https://example.com/papers/ext-001',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    discovered_at: '2026-01-01T00:00:00Z',
    priority_score: null,
    citation_count: 0,
    metadata: {},
    created_at: '2026-01-01T00:00:00Z',
    discovery_origin: 'pulse',
    user_state: null,
    recent_feedback: null,
    state: 'inbox',
    state_before_trash: null,
    starred: false,
    rating: null,
    ...overrides,
  };
}

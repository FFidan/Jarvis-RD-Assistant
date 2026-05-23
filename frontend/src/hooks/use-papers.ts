import { useQuery } from '@tanstack/react-query';
import { fetchPaperDetail, fetchFeed } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import type { SurfaceView, FeedScope } from '@/types';

/**
 * Fetch a single paper's detail by ID.
 * Wraps the `['paper-detail', id]` query key from the central registry.
 */
export function usePaperDetail(paperId: number) {
  return useQuery({
    queryKey: QUERY_KEYS.papers.detail(paperId),
    queryFn: () => fetchPaperDetail(paperId),
    enabled: !isNaN(paperId) && paperId > 0,
  });
}

/**
 * Fetch the research feed with full filter / pagination parameters.
 * Wraps the `['papers-feed', ...params]` query key from the central registry.
 */
export function usePapersFeed(params: {
  view?: SurfaceView;
  filter?: string | null;
  scope?: FeedScope;
  limit?: number;
  offset?: number;
  sourceTypes?: string | null;
}) {
  const { view, filter = null, scope, limit = 30, offset = 0, sourceTypes = null } = params;
  return useQuery({
    queryKey: QUERY_KEYS.papers.list(view ?? null, filter, scope ?? null, limit, offset, sourceTypes),
    queryFn: () => fetchFeed({ view, filter, scope, limit, offset, sourceTypes: sourceTypes ?? undefined }),
  });
}

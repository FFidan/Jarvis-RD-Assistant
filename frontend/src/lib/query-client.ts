/**
 * Shared TanStack Query client singleton.
 *
 * Exported so non-React code (e.g. the job store's SSE terminal handler)
 * can invalidate queries when background jobs finish.
 *
 * Wave 3 P1b: the read-surface slice of this cache is persisted to IndexedDB
 * for last-known-good offline read mode. `gcTime` is raised to match the
 * persist `maxAge` so dehydrated read-surface entries are not garbage-collected
 * before the offline snapshot expires (see query-persister.ts for the
 * GC_TIME >= PERSIST_MAX_AGE invariant + the dehydrate allow/deny predicate).
 * Online behaviour of non-persisted queries is unchanged: staleTime/retry/
 * refetchOnWindowFocus are untouched, only the (longer) gcTime is added.
 *
 * FE-D (security hygiene): sensitive query kinds (admin / logs / config) are
 * already excluded from IDB persistence by `shouldDehydrateQuery` and cleared
 * on logout. As a defence-in-depth layer their in-memory gcTime is overridden
 * to `SENSITIVE_GC_TIME` (~5 min) via `setQueryDefaults`, so a crash or missed
 * logout cannot leave sensitive data in the in-memory cache for the full 7-day
 * window. All non-sensitive queries keep the long gcTime required for offline.
 */
import { QueryClient } from '@tanstack/react-query';
import {
  attachQueryPersister,
  GC_TIME,
  SENSITIVE_GC_TIME,
  SENSITIVE_QUERY_KEYS,
} from './query-persister';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000, // 30 seconds
      gcTime: GC_TIME, // P1b: >= persist maxAge so offline snapshot survives
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

// FE-D: override gcTime for sensitive query families (admin / logs / config).
// `setQueryDefaults` matches any query whose queryKey *starts with* the given
// key fragment, so `['admin']` matches `['admin', 'users']`, etc. The short
// gcTime replaces the global default only for these families.
for (const key of SENSITIVE_QUERY_KEYS) {
  queryClient.setQueryDefaults([key], { gcTime: SENSITIVE_GC_TIME });
}

// P1b: attach the IndexedDB persister (idempotent). Only the offline-capable
// read surfaces are dehydrated — NON-GOAL queries (RAG/chat, pipeline,
// mutations) are excluded by query-persister's shouldDehydrateQuery predicate.
attachQueryPersister(queryClient);

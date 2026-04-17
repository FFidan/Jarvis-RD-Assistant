/**
 * Shared TanStack Query client singleton.
 *
 * Exported so non-React code (e.g. the job store's SSE terminal handler)
 * can invalidate queries when background jobs finish.
 */
import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000, // 30 seconds
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

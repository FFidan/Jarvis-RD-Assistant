/**
 * MaintenanceBanner — app-wide banner shown while `useMaintenanceStore.active`
 * is true (flipped by the `_doFetch` 503 interceptor in lib/api/core.ts).
 *
 * Recovery: polls the shared stack-health query (QUERY_KEYS.stack.health(),
 * the same cache entry HealthDots and AdminSystemHealthPage read) and clears
 * the store once the payload's `maintenance` flag/`overall` is no longer
 * 'maintenance'. Health endpoints are maintenance-exempt and return HTTP 200
 * mid-restore, so the exempt 200 status itself is never the recovery signal —
 * only the payload flag is.
 */
import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchStackHealth } from '@/lib/api';
import { useMaintenanceStore } from '@/stores/maintenance-store';

function formatStartedAt(since: number): string {
  return new Date(since).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function MaintenanceBanner() {
  const active = useMaintenanceStore((s) => s.active);
  const retryAfterS = useMaintenanceStore((s) => s.retryAfterS);
  const since = useMaintenanceStore((s) => s.since);
  const clear = useMaintenanceStore((s) => s.clear);

  const { data } = useQuery({
    queryKey: QUERY_KEYS.stack.health(),
    queryFn: fetchStackHealth,
    enabled: active,
    refetchInterval: (retryAfterS ?? 30) * 1000,
    retry: false,
  });

  useEffect(() => {
    // Clear ONLY on a definitive "restore over" signal — the health payload
    // reporting maintenance explicitly false. An 'unknown'/timeout (maintenance
    // undefined) keeps the banner and keeps polling: never clear on the ABSENCE
    // of a signal, or the banner would flip-flop mid-restore when the internal
    // health probe is briefly degraded/unreachable.
    if (data && data.maintenance === false) {
      clear();
    }
  }, [data, clear]);

  if (!active) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="maintenance-banner"
      className="flex flex-col items-center gap-0.5 px-4 py-1.5 text-xs font-medium bg-amber-50 text-amber-900 border-b border-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-800"
    >
      <span>The app is temporarily read-only while a restore is running.</span>
      <span className="text-[11px] font-normal opacity-80">
        {since !== null && `Started ${formatStartedAt(since)} · `}
        retrying automatically every {retryAfterS ?? 30}s.
      </span>
    </div>
  );
}

/**
 * use-online-status — shared connectivity primitive for the offline / PWA track.
 *
 * Consumed by:
 *   - The route-guard: MUST NOT hard-bounce to /login when `online === false`
 *     on a stale/unrefreshable session — render cached content read-only instead.
 *   - The connectivity banner + per-view offline indicators.
 *
 * Contract reference: online status behavior
 *   "Offline / PWA contract — CANONICAL" (global connectivity banner and last-known-good read mode).
 *
 * API (stable — P1c/P1d depend on this shape):
 *
 *   const { online } = useOnlineStatus();
 *
 *   - `online: boolean` — `true` when the browser reports connectivity.
 *     Derived from `navigator.onLine` and kept in sync via the `online` /
 *     `offline` window events.
 *
 * SSR / non-DOM safety: `navigator` is read lazily and guarded. When `navigator`
 * is undefined (SSR, some test envs) the hook defaults to `online: true` — the
 * conservative choice (do not show an offline banner / do not block reads when
 * connectivity is simply unknowable). `navigator.onLine === false` is a reliable
 * negative; `true` can be a false positive (captive portal etc.) — fetch-failure
 * detection that refines this lives in P1d, layered on top of this primitive.
 */
import { useEffect, useState } from 'react';

export interface OnlineStatus {
  /** `true` when the browser reports connectivity (navigator.onLine). */
  online: boolean;
}

/** Read `navigator.onLine` defensively; default to online when unknowable. */
function readOnline(): boolean {
  if (typeof navigator === 'undefined' || typeof navigator.onLine !== 'boolean') {
    return true;
  }
  return navigator.onLine;
}

/**
 * Subscribe to browser connectivity changes.
 *
 * @returns `{ online }` — re-renders the consumer on `online`/`offline` events.
 */
export function useOnlineStatus(): OnlineStatus {
  const [online, setOnline] = useState<boolean>(readOnline);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }
    const handleOnline = () => setOnline(true);
    const handleOffline = () => setOnline(false);

    // Re-sync on mount in case connectivity changed before the listeners
    // were attached (e.g. during hydration).
    setOnline(readOnline());

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  return { online };
}

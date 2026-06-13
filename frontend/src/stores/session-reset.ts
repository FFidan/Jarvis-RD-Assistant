/**
 * Logout-reset registry.
 *
 * User-scoped stores register a callback that clears their in-memory state, and
 * `auth-store.logout()` invokes them all via `runSessionResets()` WITHOUT
 * importing any store. Inverting the dependency this way means:
 *   - auth-store no longer enumerates the store catalogue (single responsibility);
 *   - no `auth-store -> job-store` import is created, so the `job-store -> auth-store`
 *     dependency (job-store reads auth state at call time) stays a one-way edge —
 *     no import cycle; and
 *   - the stores remain statically imported by their components only, so there is
 *     no mixed static/dynamic import for the bundler to refuse to code-split.
 *
 * Registration runs as a module-init side effect: a store is reset on logout only
 * if it was imported during the session — which it is, if it holds any state.
 */
type SessionResetFn = () => void;

const resets = new Set<SessionResetFn>();

export function registerSessionReset(fn: SessionResetFn): void {
  resets.add(fn);
}

export function runSessionResets(): void {
  for (const reset of resets) {
    try {
      reset();
    } catch (e) {
      // Best-effort: one store's reset failing must not block the others.
      console.warn('[session-reset] a store reset failed', e);
    }
  }
}

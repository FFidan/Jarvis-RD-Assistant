/**
 * Polyfill for AbortSignal.any (pre-2024 browser compat).
 *
 * AbortSignal.any() combines multiple signals — the resulting signal aborts
 * as soon as any of the input signals aborts.
 *
 * Browser support: Chrome 116+, Firefox 124+, Safari 17.4+ (released 2023-2024).
 * Older browsers (and jsdom in tests) need this polyfill.
 *
 * Import this module ONCE, as early as possible (before any code that uses
 * AbortSignal.any). Safe to call multiple times — idempotent.
 */
export function applyAbortSignalAnyPolyfill(): void {
  if (typeof AbortSignal !== 'undefined' && !('any' in AbortSignal)) {
    (AbortSignal as { any?: unknown }).any = function any(
      signals: AbortSignal[],
    ): AbortSignal {
      const ctrl = new AbortController();
      for (const s of signals) {
        if (s.aborted) {
          ctrl.abort(s.reason);
          return ctrl.signal;
        }
        s.addEventListener('abort', () => ctrl.abort(s.reason), { once: true });
      }
      return ctrl.signal;
    };
  }
}

// Apply immediately on import so callers don't need to call the function.
applyAbortSignalAnyPolyfill();

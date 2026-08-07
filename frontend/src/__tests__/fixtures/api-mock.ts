/** Shared mock for the `@/lib/api` module. */
import { vi } from 'vitest';

type ApiSurface = typeof import('@/lib/api');

/**
 * Per-export default implementations. Each override is a plain function; it
 * is wrapped in `vi.fn(impl)` so the global `mockReset: true` restores it
 * before every test instead of wiping it to `undefined` (which is what
 * happens to `vi.fn().mockResolvedValue(...)` chains).
 *
 * Keys are checked against the real module surface (typo protection);
 * signatures are deliberately loose — test stubs return shapes as partial as
 * the assertions need, exactly as the per-file `vi.fn()` mocks always have.
 */
export type ApiOverrides = Partial<Record<keyof ApiSurface, (...args: never[]) => unknown>>;

/**
 * Build a full `@/lib/api` module mock.
 *
 * Usage (the factory must dynamically import this module because `vi.mock`
 * is hoisted):
 *
 *   vi.mock('@/lib/api', async () =>
 *     (await import('@/__tests__/fixtures/api-mock')).createApiMock({
 *       fetchFeed: async () => ({ papers: [], total: 0 }),
 *     }));
 *
 * Every function export exists on the mock: overridden ones carry the given
 * implementation as their reset-safe default, all others reject with a
 * descriptive error when called, so a component reaching for an unstubbed
 * endpoint surfaces as a normal failed request (error/degraded render path)
 * instead of a confusing `undefined` return. `ApiError` and non-function
 * exports are passed through real, so `instanceof` checks in tests and
 * production keep working.
 */
// Pure synchronous helpers on the api surface. They stay real: replacing a
// sync formatter/comparator with an async rejecting stub would hand a Promise
// to JSX/sort call sites, which React rejects as an "async Client Component".
const PURE_SYNC_EXPORTS = new Set(['ApiError', 'cloudProviderLabel', 'compareCloudProviders']);

export async function createApiMock(overrides: ApiOverrides = {}): Promise<ApiSurface> {
  const actual = await vi.importActual<ApiSurface>('@/lib/api');
  const mocked: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(actual)) {
    if (PURE_SYNC_EXPORTS.has(key) || typeof value !== 'function') {
      mocked[key] = value;
    } else {
      mocked[key] = vi.fn(async () => {
        throw new Error(`api.${key} is not stubbed by this test`);
      });
    }
  }
  for (const [key, impl] of Object.entries(overrides)) {
    mocked[key] = vi.fn(impl as (...args: unknown[]) => unknown);
  }
  return mocked as ApiSurface;
}

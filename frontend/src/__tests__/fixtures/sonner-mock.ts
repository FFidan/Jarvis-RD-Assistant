/** Shared mock for the `sonner` toast library. */
import { vi } from 'vitest';

/**
 * Build a complete `sonner` module mock.
 *
 * Usage (the factory must dynamically import this module because `vi.mock`
 * is hoisted):
 *
 *   vi.mock('sonner', async () =>
 *     (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());
 *
 * The mock covers every `toast.*` member production code calls (error,
 * success, warning, info, message). Completeness is load-bearing, not
 * cosmetic: several production `toast.warning` call sites sit inside catch
 * blocks, so a mock missing a member does not fail the test — the resulting
 * TypeError is swallowed and the cleanup work after the call is silently
 * skipped. Never trim members from this surface.
 *
 * All members are bare `vi.fn()` (call recorders), so the global
 * `mockReset: true` leaves their behavior unchanged between tests.
 */
export function createSonnerMock() {
  return {
    toast: {
      error: vi.fn(),
      success: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
      message: vi.fn(),
    },
    // Rendered by @/components/ui/toaster; a null component keeps App-level
    // renders working under this mock.
    Toaster: (): null => null,
  };
}

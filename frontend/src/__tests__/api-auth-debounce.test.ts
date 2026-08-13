/**
 * Session-expired toast debounce guard (Task B2-T5).
 *
 * The auto-logout-on-401 handler (`handleAuthFailure` in `lib/api/core`) keeps
 * a module-scoped `_sessionExpiredToastShownAt` singleton so a burst of
 * parallel requests that all 401 at once shows ONE "Session expired" toast, not
 * one per request, within a 5s window. This singleton must survive the
 * god-module → domain-modules split, so we exercise it through the public
 * status-only request surface (the handler itself is intentionally not exported).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mocked before importing the module under test so the singleton sees them.
vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: () => ({
      isAuthenticated: true,
      logout: vi.fn(),
    }),
  },
}));

import { apiFetchVoid } from '@/lib/api';
import { toast } from 'sonner';

describe('handleAuthFailure session-expired toast debounce', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.mocked(toast.error).mockClear();
  });

  it('shows exactly one toast for a burst of parallel 401s within the 5s window', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('unauthorized', { status: 401 }),
    );

    // Fire several requests that all 401 "at once" (same debounce window).
    const results = await Promise.allSettled([
      apiFetchVoid('/api/a'),
      apiFetchVoid('/api/b'),
      apiFetchVoid('/api/c'),
      apiFetchVoid('/api/d'),
    ]);

    // All reject with ApiError(401)…
    expect(results.every((r) => r.status === 'rejected')).toBe(true);
    // …but the debounce singleton collapses the burst to a single toast.
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringMatching(/session expired/i),
      expect.objectContaining({ duration: 6000 }),
    );
  });

  it('does not toast on a 403 (permission denied, not session expiry)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('forbidden', { status: 403 }),
    );

    await apiFetchVoid('/api/forbidden').catch(() => undefined);

    expect(toast.error).not.toHaveBeenCalled();
  });
});

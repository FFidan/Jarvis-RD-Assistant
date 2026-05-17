/**
 * query-client — gcTime hygiene tests (FE-D).
 *
 * Verifies that:
 *   1. Sensitive query kinds (admin / logs / config) resolve to SENSITIVE_GC_TIME
 *      via `setQueryDefaults` — defence-in-depth hygiene gap fix.
 *   2. Normal read-surface query kinds keep the long GC_TIME required for the
 *      offline snapshot (P1b invariant: gcTime >= PERSIST_MAX_AGE).
 *
 * We import the *already-constructed* queryClient singleton (same instance the
 * app uses) to verify the defaults were applied at construction time, not just
 * that the code "runs". Using `getQueryDefaults` is the idiomatic TanStack
 * Query approach for inspecting per-key defaults without side-effecting queries.
 */
import { describe, it, expect } from 'vitest';
import { queryClient } from '@/lib/query-client';
import {
  GC_TIME,
  SENSITIVE_GC_TIME,
  SENSITIVE_QUERY_KEYS,
} from '@/lib/query-persister';

describe('queryClient gcTime defaults — FE-D sensitive-key hygiene', () => {
  it('SENSITIVE_GC_TIME < GC_TIME (sanity: short really is shorter)', () => {
    expect(SENSITIVE_GC_TIME).toBeLessThan(GC_TIME);
  });

  it.each(SENSITIVE_QUERY_KEYS.map((k) => [k]))(
    'sensitive key "%s" resolves to SENSITIVE_GC_TIME',
    (key) => {
      // TanStack Query merges defaults from the most-specific match outward.
      // A query with queryKey [key, 'some-sub-key'] should inherit the override.
      const defaults = queryClient.getQueryDefaults([key, 'sub']);
      expect(defaults?.gcTime).toBe(SENSITIVE_GC_TIME);
    },
  );

  it.each([
    ['papers-feed'],
    ['paper-detail'],
    ['notes'],
    ['dashboard-metrics'],
    ['decks'],
    ['cards'],
    ['my-day'],
    ['projects'],
    ['citation-graph'],
    ['knowledge-graph'],
  ])(
    'read-surface key "%s" keeps the long GC_TIME (offline snapshot intact)',
    (key) => {
      const defaults = queryClient.getQueryDefaults([key]);
      // `getQueryDefaults` returns `undefined` when no per-key override was set.
      // The global default (set in `defaultOptions`) applies; we verify the key
      // is NOT overridden to SENSITIVE_GC_TIME.
      expect(defaults?.gcTime).not.toBe(SENSITIVE_GC_TIME);
      // Additionally confirm the global default itself is the long GC_TIME.
      const globalGcTime =
        queryClient.getDefaultOptions().queries?.gcTime;
      expect(globalGcTime).toBe(GC_TIME);
    },
  );

  it('sensitive admin key with a realistic sub-key also gets SENSITIVE_GC_TIME', () => {
    const defaults = queryClient.getQueryDefaults(['admin', 'users']);
    expect(defaults?.gcTime).toBe(SENSITIVE_GC_TIME);
  });

  it('sensitive logs key with event sub-key also gets SENSITIVE_GC_TIME', () => {
    const defaults = queryClient.getQueryDefaults(['logs', 'events']);
    expect(defaults?.gcTime).toBe(SENSITIVE_GC_TIME);
  });

  it('sensitive config key also gets SENSITIVE_GC_TIME', () => {
    const defaults = queryClient.getQueryDefaults(['config']);
    expect(defaults?.gcTime).toBe(SENSITIVE_GC_TIME);
  });
});

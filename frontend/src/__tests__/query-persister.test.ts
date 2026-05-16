/**
 * query-persister (Wave 3 P1b) — last-known-good IDB cache.
 *
 * Coverage:
 *   - shouldDehydrateQuery: includes read-surface keys, excludes NON-GOAL keys,
 *     excludes non-success / no-data states, rejects malformed keys.
 *   - persister round-trip: dehydrate read surface → restore into a fresh
 *     QueryClient; NON-GOAL queries are NOT restored.
 *   - clearPersistedQueryCache: empties IDB + posts the SW JARVIS_LOGOUT msg.
 *   - getPersistedCacheTimestamp: null before any persist, epoch-ms after.
 *
 * `fake-indexeddb/auto` (devDep, test-only) installs a spec-compliant
 * IndexedDB into the jsdom global so idb-keyval performs a *real* round-trip
 * (jsdom ships no IndexedDB). Scoped to this file — the shared test setup is
 * intentionally untouched (no global blast radius).
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { QueryClient, type Query } from '@tanstack/react-query';
import { clear as idbClear } from 'idb-keyval';
import {
  attachQueryPersister,
  clearPersistedQueryCache,
  flushPersistedQueryCache,
  getPersistedCacheTimestamp,
  shouldDehydrateQuery,
  __resetPersisterForTests,
} from '@/lib/query-persister';

/** Build a minimal Query-like object for the predicate (no real fetch). */
function fakeQuery(
  queryKey: unknown[],
  status: 'success' | 'pending' | 'error' = 'success',
  data: unknown = { ok: true },
): Query {
  return {
    queryKey,
    state: { status, data: status === 'success' ? data : undefined },
  } as unknown as Query;
}

/** Wait until a condition holds (persistQueryClient writes are throttled). */
async function waitFor(fn: () => boolean | Promise<boolean>, ms = 3000) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (await fn()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error('waitFor timed out');
}

beforeEach(async () => {
  __resetPersisterForTests();
  await idbClear().catch(() => {});
  await clearPersistedQueryCache().catch(() => {});
  __resetPersisterForTests();
  await idbClear().catch(() => {});
});

describe('shouldDehydrateQuery — read-surface allow-list', () => {
  it.each([
    ['papers-feed'],
    ['papers-brief'],
    ['feed-counts'],
    ['paper-detail'],
    ['notes'],
    ['extraction-table'],
    ['dashboard-metrics'],
    ['retention-stats'],
    ['decks'],
    ['cards'],
    ['my-day'],
    ['projects'],
    ['citation-graph'],
    ['knowledge-graph'],
  ])('persists read surface %s', (key) => {
    expect(shouldDehydrateQuery(fakeQuery([key, 1]))).toBe(true);
  });
});

describe('shouldDehydrateQuery — NON-GOAL exclusions', () => {
  it.each([
    ['chat'],
    ['ask'],
    ['pulse-explain'],
    ['discover'],
    ['contradictions'],
    ['jobs'],
    ['logs'],
    ['stack-health'],
    ['system-models'],
    ['admin'],
    ['account'],
    ['config'],
    ['pairing-status'],
    ['first-run-status'],
    ['setup-status'],
  ])('never persists NON-GOAL %s', (key) => {
    expect(shouldDehydrateQuery(fakeQuery([key, 1]))).toBe(false);
  });

  it('excludes unknown keys (default DENY)', () => {
    expect(shouldDehydrateQuery(fakeQuery(['totally-unknown-key']))).toBe(
      false,
    );
  });

  it('excludes pending / error states even on a read surface', () => {
    expect(shouldDehydrateQuery(fakeQuery(['paper-detail', 1], 'pending'))).toBe(
      false,
    );
    expect(shouldDehydrateQuery(fakeQuery(['paper-detail', 1], 'error'))).toBe(
      false,
    );
  });

  it('rejects malformed / non-string-first queryKeys', () => {
    expect(shouldDehydrateQuery(fakeQuery([42, 'x']))).toBe(false);
    expect(shouldDehydrateQuery(fakeQuery([]))).toBe(false);
  });
});

describe('persister round-trip (dehydrate -> restore)', () => {
  it('restores read-surface queries but NOT NON-GOAL queries', async () => {
    const source = new QueryClient({
      defaultOptions: { queries: { gcTime: 1_000_000 } },
    });
    const unsub = attachQueryPersister(source);
    // One read surface (should persist) + one NON-GOAL (must not).
    source.setQueryData(['paper-detail', 77], { title: 'Neural ODEs' });
    source.setQueryData(['chat', 77], { messages: ['secret'] });
    // Deterministic flush (steady-state path is the throttled subscription).
    await flushPersistedQueryCache();
    expect(await getPersistedCacheTimestamp()).not.toBeNull();
    unsub();

    // Fresh client + fresh module singletons -> restore from IDB.
    __resetPersisterForTests();
    const restored = new QueryClient({
      defaultOptions: { queries: { gcTime: 1_000_000 } },
    });
    const unsub2 = attachQueryPersister(restored);
    // persistQueryClient restores asynchronously on attach.
    await waitFor(
      () => restored.getQueryData(['paper-detail', 77]) !== undefined,
    );
    unsub2();

    expect(restored.getQueryData(['paper-detail', 77])).toEqual({
      title: 'Neural ODEs',
    });
    // NON-GOAL query must never have been dehydrated, so never restored.
    expect(restored.getQueryData(['chat', 77])).toBeUndefined();
  });
});

describe('clearPersistedQueryCache', () => {
  it('empties IDB and posts the SW JARVIS_LOGOUT message', async () => {
    const post = vi.fn();
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: { controller: { postMessage: post } },
    });

    const c = new QueryClient({
      defaultOptions: { queries: { gcTime: 1_000_000 } },
    });
    const unsub = attachQueryPersister(c);
    c.setQueryData(['paper-detail', 1], { x: 1 });
    await flushPersistedQueryCache();
    expect(await getPersistedCacheTimestamp()).not.toBeNull();
    unsub();

    await clearPersistedQueryCache();

    expect(await getPersistedCacheTimestamp()).toBeNull();
    expect(post).toHaveBeenCalledWith({ type: 'JARVIS_LOGOUT' });

    // @ts-expect-error — tidy the stub we installed.
    delete navigator.serviceWorker;
  });

  it('does not throw when no service worker controls the page', async () => {
    await expect(clearPersistedQueryCache()).resolves.toBeUndefined();
  });
});

describe('getPersistedCacheTimestamp', () => {
  it('is null before any persist, epoch-ms after', async () => {
    expect(await getPersistedCacheTimestamp()).toBeNull();

    const before = Date.now();
    const c = new QueryClient({
      defaultOptions: { queries: { gcTime: 1_000_000 } },
    });
    const unsub = attachQueryPersister(c);
    c.setQueryData(['papers-feed', 'a'], [{ id: 1 }]);
    await flushPersistedQueryCache();
    unsub();

    const ts = await getPersistedCacheTimestamp();
    expect(typeof ts).toBe('number');
    expect(ts as number).toBeGreaterThanOrEqual(before);
  });
});

/**
 * query-persister — last-known-good IDB cache.
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
  SENSITIVE_QUERY_KEYS,
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

describe('SENSITIVE_QUERY_KEYS — FE-GC-1: smtp-config + telegram-bot-token-status', () => {
  it("contains 'smtp-config'", () => {
    expect(SENSITIVE_QUERY_KEYS).toContain('smtp-config');
  });

  it("contains 'telegram-bot-token-status'", () => {
    expect(SENSITIVE_QUERY_KEYS).toContain('telegram-bot-token-status');
  });

  it.each([['smtp-config'], ['telegram-bot-token-status']])(
    "'%s' is excluded from IDB dehydration (shouldDehydrateQuery)",
    (key) => {
      expect(shouldDehydrateQuery(fakeQuery([key]))).toBe(false);
    },
  );
});

describe("notes — offline read surface + shared-device purge (L-6)", () => {
  /**
   * Notes are user-authored private content kept in the offline allow-list so
   * the "read-only offline" UX works (PaperResearchLog renders NotesTab with
   * readOnly={!isOnline}). The security invariant is that clearPersistedQueryCache()
   * removes them from IDB before the next user can load a stale snapshot.
   */
  it("shouldDehydrateQuery accepts 'notes' (offline read surface is intentional)", () => {
    expect(shouldDehydrateQuery(fakeQuery(['notes', 1, 'user']))).toBe(true);
    expect(shouldDehydrateQuery(fakeQuery(['notes', 1, 'zotero']))).toBe(true);
  });

  it("notes persisted to IDB are erased by clearPersistedQueryCache (shared-device guard)", async () => {
    // Persist a notes query into IDB.
    const source = new QueryClient({
      defaultOptions: { queries: { gcTime: 1_000_000 } },
    });
    const unsub = attachQueryPersister(source);
    source.setQueryData(['notes', 42, 'user'], [{ id: 1, user_note: 'private' }]);
    await flushPersistedQueryCache();
    // Confirm something was persisted (timestamp is set).
    expect(await getPersistedCacheTimestamp()).not.toBeNull();
    unsub();

    // Simulate logout: purge IDB.
    await clearPersistedQueryCache();

    // After purge, IDB timestamp is gone — the snapshot (including notes) is cleared.
    expect(await getPersistedCacheTimestamp()).toBeNull();

    // A freshly attached client must NOT restore the notes entry.
    __resetPersisterForTests();
    const restored = new QueryClient({
      defaultOptions: { queries: { gcTime: 1_000_000 } },
    });
    const unsub2 = attachQueryPersister(restored);
    // Give persistQueryClient time to attempt a restore (it's async).
    await new Promise((r) => setTimeout(r, 100));
    unsub2();

    // Notes must be absent — purge erased the entire IDB snapshot.
    expect(restored.getQueryData(['notes', 42, 'user'])).toBeUndefined();
  });
});

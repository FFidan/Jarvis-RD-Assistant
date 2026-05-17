/**
 * query-persister — last-known-good offline cache for TanStack Query (Wave 3 P1b).
 *
 * Contract reference:
 *   docs/superpowers/specs/2026-05-15-shell-sidebar-admin-ia-redesign-design.md
 *   "Offline / PWA contract — CANONICAL" §3 (Client cache) + §4 (last-known-good)
 *   + "Explicit offline NON-GOALS" + the per-surface offline table.
 *
 * What this does
 * --------------
 * Persists ONLY the offline-capable *read-surface* slice of the in-memory
 * TanStack Query cache to IndexedDB so those queries survive a reload while
 * offline (last-known-good read mode). Online behaviour/semantics of
 * non-persisted queries are untouched — the persister only ever *reads* the
 * cache for dehydration and only ever *restores* on boot.
 *
 * NON-GOALS are excluded twice over (canonical "Explicit offline NON-GOALS"):
 *   - dehydrate predicate skips them on the way out, AND
 *   - they are simply absent from the read-surface allow-list (default DENY).
 * Excluded: RAG/chat/ask, discovery/fetch/process/embedding/summarize,
 * contradictions, any mutation cache, and operational/admin/auth surfaces.
 *
 * Dependency-owner notes (P1b is the sole Wave-3 dep owner)
 * --------------------------------------------------------
 *   - `idb-keyval` (~0.6kB) backs a minimal async store. Chosen over a
 *     hand-rolled IDB AsyncStorage: it correctly handles DB open/upgrade,
 *     `blocked` events and transaction lifecycle with a far smaller, audited
 *     footprint than a correct hand-roll would have.
 *   - `@tanstack/query-async-storage-persister` (not the *sync* storage
 *     persister) because IDB access is inherently async — the sync persister
 *     cannot await idb-keyval.
 *
 * Public API (STABLE — P1c/P1d consume these)
 * -------------------------------------------
 *   - `attachQueryPersister(client)` — create + attach the IDB persister to
 *     the existing global QueryClient. Idempotent. Returns an unsubscribe fn.
 *   - `clearPersistedQueryCache(): Promise<void>` — P1c calls this from the
 *     logout path. Purges the IDB-persisted cache AND posts the P1a
 *     `JARVIS_LOGOUT` message so the SW runtime cache is purged too
 *     (cross-user data hygiene — both stores cleared in one call).
 *   - `getPersistedCacheTimestamp(): Promise<number | null>` — epoch ms of the
 *     last successful persist (P1d renders "stale-cached · as of T"); `null`
 *     when nothing has been persisted yet.
 */

import type { QueryClient, Query } from '@tanstack/react-query';
import {
  persistQueryClient,
  persistQueryClientSave,
} from '@tanstack/react-query-persist-client';
import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister';
import {
  get as idbGet,
  set as idbSet,
  del as idbDel,
  createStore,
  type UseStore,
} from 'idb-keyval';

/* ---- cache lifetime knobs ------------------------------------------------ */

/**
 * `PERSIST_MAX_AGE` — how long a persisted snapshot is considered restorable.
 * `GC_TIME` — how long an individual query entry is retained in the (in-memory
 * + dehydrated) cache before garbage collection.
 *
 * Rationale: for last-known-good read mode a researcher may reopen the PWA on a
 * tablet after being offline for days. We want a long survival window for the
 * read surfaces. CRITICAL invariant: `GC_TIME >= PERSIST_MAX_AGE`. If gcTime
 * were shorter, a query would be GC'd out of the dehydrated snapshot before
 * `maxAge` elapsed, defeating the persistence. We set them equal at 7 days so
 * the snapshot and the entries expire together, deliberately.
 */
export const PERSIST_MAX_AGE = 7 * 24 * 60 * 60 * 1000; // 7 days
export const GC_TIME = PERSIST_MAX_AGE; // must be >= PERSIST_MAX_AGE

/**
 * `SENSITIVE_GC_TIME` — short in-memory TTL for sensitive query kinds
 * (admin / logs / config). These are already excluded from IDB persistence by
 * `shouldDehydrateQuery` and cleared on logout, so this is an additional
 * defence-in-depth layer: a session crash or missed logout cannot leave
 * sensitive data in the in-memory cache for days. 5 minutes is enough for any
 * realistic UI interaction while keeping no stale sensitive state across a
 * browser idle period.
 *
 * The corresponding `setQueryDefaults` calls in `query-client.ts` apply this
 * to queries whose `queryKey[0]` is `'admin'`, `'logs'`, or `'config'`.
 */
export const SENSITIVE_GC_TIME = 5 * 60 * 1000; // 5 minutes

/**
 * Query-key families whose entries should use `SENSITIVE_GC_TIME`.
 * Kept here (co-located with NON_GOAL_KEYS) so audits have a single place to
 * check: exclude from IDB + short-lived in memory.
 *
 * Exported so `query-client.ts` can register `setQueryDefaults` for each.
 */
export const SENSITIVE_QUERY_KEYS: ReadonlyArray<string> = [
  'admin',
  'logs',
  'config',
] as const;

/** Bumped when the dehydrated shape changes — invalidates old snapshots. */
const PERSIST_BUSTER = 'jarvis-qp-v1';

/* ---- IndexedDB-backed async store --------------------------------------- */

const IDB_DB_NAME = 'jarvis-query-cache';
const IDB_STORE_NAME = 'tanstack-query';
/** Single key under which the dehydrated client is stored. */
const PERSIST_KEY = 'jarvis-react-query';
/** Companion key holding the epoch-ms of the last successful persist. */
const TIMESTAMP_KEY = 'jarvis-react-query:ts';

let _store: UseStore | null = null;
function store(): UseStore {
  if (_store === null) {
    _store = createStore(IDB_DB_NAME, IDB_STORE_NAME);
  }
  return _store;
}

/**
 * Minimal `AsyncStorage` over idb-keyval, matching the shape the TanStack
 * async-storage persister expects (`getItem`/`setItem`/`removeItem`).
 * `setItem` additionally records the persist timestamp so P1d can surface it.
 */
const idbAsyncStorage = {
  getItem: async (key: string): Promise<string | null> => {
    const v = await idbGet<string>(key, store());
    return v ?? null;
  },
  setItem: async (key: string, value: string): Promise<void> => {
    await idbSet(key, value, store());
    // Stamp every successful write so the freshness indicator is accurate.
    await idbSet(TIMESTAMP_KEY, Date.now(), store());
  },
  removeItem: async (key: string): Promise<void> => {
    await idbDel(key, store());
    await idbDel(TIMESTAMP_KEY, store());
  },
};

/* ---- read-surface allow-list (queryKey-keyed) ---------------------------- */

/**
 * Offline-capable read surfaces, keyed on the *first* element of the queryKey
 * (the app's stable convention — see the queryKey inventory in
 * `src/**`). This mirrors the spirit of P1a's `sw-cache-policy.ts` SAFELIST
 * but operates on queryKeys rather than URLs (dehydration filters by key).
 *
 * Canonical per-surface table → keys:
 *   - Library / paper browsing  → papers-feed, papers-brief, feed-counts
 *   - Paper Detail (meta/abstract/summary/chunks) → paper-detail
 *   - Notes (READ only)         → notes
 *   - Structured extractions    → extraction-table, extraction-templates
 *   - Glanceable read aggregates/stats → dashboard-metrics, retention-stats,
 *     card-stats, decks, cards (cached deck/stats read-only per Learning Cards
 *     P1 row)
 *   - My-Day last-known snapshot (read-only) → my-day
 *   - Projects read metadata the reading surfaces hydrate → project*,
 *     citation-graph, knowledge-graph
 */
const READ_SURFACE_KEYS: ReadonlySet<string> = new Set<string>([
  // Library / feed browsing
  'papers-feed',
  'papers-brief',
  'feed-counts',
  'feed',
  // Single paper detail (metadata / abstract / summary / chunks)
  'paper-detail',
  // Notes — READ ONLY (offline note editing is an explicit NON-GOAL; only the
  // GET-populated read cache is persisted, mutations never enter dehydration).
  'notes',
  // Structured extractions (read table + its template list)
  'extraction-table',
  'extraction-templates',
  // Glanceable read aggregates / stats
  'dashboard-metrics',
  'retention-stats',
  'card-stats',
  // Learning Cards P1: cached deck/stats read-only
  'decks',
  'cards',
  // My-Day low-priority cached last-known snapshot (read-only)
  'my-day',
  // Read metadata the reading surfaces hydrate
  'projects',
  'project',
  'project-papers',
  'project-questions',
  'project-activity',
  'citation-graph',
  'knowledge-graph',
]);

/**
 * Explicit NON-GOAL deny-list (belt-and-braces, mirrors P1a DENYLIST intent).
 * Any queryKey whose first element is here is never persisted even if a future
 * allow-list edit accidentally matches. Covers RAG/chat, the model/pipeline
 * layer, contradictions, operational/admin/auth surfaces, and live polling.
 */
const NON_GOAL_KEYS: ReadonlySet<string> = new Set<string>([
  // RAG / chat / cross-paper Q&A
  'chat',
  'ask',
  'pulse-explain',
  // Model / pipeline layer (discovery / fetch / process / embedding)
  'discover',
  'contradictions',
  'recent-feedback',
  'feedback-summary',
  // Live / volatile operational state
  'jobs',
  'logs',
  'stack-health',
  'pulse-debug',
  'system-models',
  'pairing-status',
  'pairing-status-initial',
  'first-run-status',
  'setup-status',
  // Admin / auth lifecycle (security — never persist cross-user)
  'admin',
  'account',
  'config',
]);

/**
 * Dehydrate predicate: a query is persisted iff its first queryKey element is
 * an allow-listed read surface, it is NOT on the NON-GOAL deny-list, and it
 * actually has data in a success state (no point persisting errors/pending).
 *
 * Exported for unit tests + future audits. Pure; do not mutate inputs.
 */
export function shouldDehydrateQuery(query: Query): boolean {
  const key0 = Array.isArray(query.queryKey) ? query.queryKey[0] : undefined;
  if (typeof key0 !== 'string') {
    return false;
  }
  // Belt-and-braces: any NON-GOAL match disqualifies regardless of allow-list.
  if (NON_GOAL_KEYS.has(key0)) {
    return false;
  }
  // Default DENY: must positively match an offline-capable read surface.
  if (!READ_SURFACE_KEYS.has(key0)) {
    return false;
  }
  // Only persist queries that successfully resolved with data — never persist
  // pending/error states (they would restore as stale failures offline).
  return query.state.status === 'success' && query.state.data !== undefined;
}

/* ---- public API ---------------------------------------------------------- */

let _unsubscribe: (() => void) | null = null;
let _persister: ReturnType<typeof createAsyncStoragePersister> | null = null;
let _client: QueryClient | null = null;

/**
 * Create + attach the IDB persister to the existing global QueryClient.
 * Idempotent: a second call returns the same unsubscribe handle without
 * re-attaching. Restores any prior snapshot, then keeps it in sync.
 *
 * Online behaviour of non-persisted queries is unaffected — this only adds a
 * dehydration *filter* + restore; it never changes fetch/retry/staleness.
 *
 * Degrades gracefully when IndexedDB is unavailable (Safari private mode,
 * locked-down enterprise, SSR, jsdom without fake-indexeddb): returns a no-op
 * unsubscribe without attaching or throwing. Last-known-good persistence is
 * simply unavailable in that environment; all other app behaviour is unaffected.
 */
export function attachQueryPersister(client: QueryClient): () => void {
  // Guard: IDB unavailable (private mode, enterprise lock-down, SSR, test
  // environments that have not polyfilled it). Return a no-op so callers at
  // module-import time (query-client.ts) never throw an unhandled error.
  if (typeof indexedDB === 'undefined') {
    return () => {};
  }

  if (_unsubscribe !== null) {
    return _unsubscribe;
  }

  const persister = createAsyncStoragePersister({
    storage: idbAsyncStorage,
    key: PERSIST_KEY,
    // Keep IO small; throttle bursts of cache writes.
    throttleTime: 1000,
  });

  const [unsubscribe] = persistQueryClient({
    queryClient: client,
    persister,
    maxAge: PERSIST_MAX_AGE,
    buster: PERSIST_BUSTER,
    dehydrateOptions: {
      shouldDehydrateQuery,
    },
  });

  _persister = persister;
  _client = client;
  _unsubscribe = unsubscribe;
  return unsubscribe;
}

/**
 * Force an immediate (un-throttled) persist of the current read-surface cache
 * slice to IndexedDB. The steady-state path is the throttled cache
 * subscription set up by {@link attachQueryPersister}; this is an explicit
 * flush for callers that need a synchronous-after-await guarantee (and for
 * deterministic tests). No-op if the persister is not attached.
 */
export async function flushPersistedQueryCache(): Promise<void> {
  if (_persister === null || _client === null) return;
  try {
    await persistQueryClientSave({
      queryClient: _client,
      persister: _persister,
      buster: PERSIST_BUSTER,
      dehydrateOptions: { shouldDehydrateQuery },
    });
  } catch (err) {
    console.warn('[query-persister] flush failed', err);
  }
}

/**
 * Purge the persisted query cache from IndexedDB AND tell the active service
 * worker to drop its runtime API cache (P1a `JARVIS_LOGOUT` contract).
 *
 * P1c calls this from the logout path. Both the client cache (IDB) and the SW
 * runtime cache must be cleared together so the next user on a shared device
 * never sees the previous user's cached data (cross-user hygiene — P1a flagged
 * this dependency). Best-effort + non-throwing: a storage failure must not
 * block logout.
 */
export async function clearPersistedQueryCache(): Promise<void> {
  try {
    await idbDel(PERSIST_KEY, store());
    await idbDel(TIMESTAMP_KEY, store());
  } catch (err) {
    console.warn('[query-persister] IDB purge failed', err);
  }
  // Mirror the existing auth-store contract: tell the SW to purge its
  // per-user runtime cache too. Optional-chained — no-op if no SW controls
  // this page yet (dev / first load).
  try {
    if (typeof navigator !== 'undefined') {
      navigator.serviceWorker?.controller?.postMessage({
        type: 'JARVIS_LOGOUT',
      });
    }
  } catch (err) {
    console.warn('[query-persister] SW logout postMessage failed', err);
  }
}

/**
 * Epoch-ms of the last successful cache persist, or `null` if nothing has been
 * persisted yet. P1d renders the "stale-cached · as of T" freshness affordance
 * from this value. Non-throwing — resolves `null` on any read failure.
 */
export async function getPersistedCacheTimestamp(): Promise<number | null> {
  try {
    const ts = await idbGet<number>(TIMESTAMP_KEY, store());
    return typeof ts === 'number' ? ts : null;
  } catch (err) {
    console.warn('[query-persister] timestamp read failed', err);
    return null;
  }
}

/** Test-only: reset the module-level singletons between specs. */
export function __resetPersisterForTests(): void {
  _unsubscribe = null;
  _persister = null;
  _client = null;
  _store = null;
}

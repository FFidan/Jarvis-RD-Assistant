/**
 * review-outbox — append-only IndexedDB outbox for offline flashcard reviews.
 *
 * The ONLY offline-write surface — flashcard review.
 *
 * Contract references:
 *   - offline review sync contract
 *     §Offline / PWA contract → "P2 (offline review + sync) — the flow this spec owns".
 *   - offline review sync contract
 *     "Offline / PWA contract — CANONICAL" → flashcard review = only offline-write;
 *     sync = idempotent batch replay → server recomputes FSRS; reconcile = a
 *     SINGLE toast "N synced, M skipped" (no merge UI).
 *   - The wire/idempotency contract this client implements is specified for the
 *     functional/backend track in
 *     offline review sync contract.
 *
 * What this does
 * --------------
 * When a review is rated while OFFLINE, the rating is appended to an IndexedDB
 * outbox instead of POSTing the live per-card endpoint. Each record carries a
 * client-generated idempotency key (uuid) so the server can replay-dedupe, the
 * card_id, the rating, a client timestamp, and the *current user identity*.
 * On reconnect the outbox is drained as a single idempotent batch; synced and
 * skipped entries are removed, failures are retained for the next attempt (no
 * data loss, no spam).
 *
 * Cross-user safety (mandatory — WITHOUT touching auth-store)
 * ----------------------------------------------------------
 * The active user identity is read from the auth store via its PUBLIC selector
 * (`useAuthStore.getState().getUser()`) — imported and read only, never mutated.
 * Every record is stamped with `user_id`. All reads/drains filter to the active
 * user, and `purgeForeignEntries()` (invoked at every enqueue and drain) deletes
 * any entry whose `user_id` differs from the active identity. Net effect: user B
 * never sees, drains, or syncs user A's queued reviews even on a shared device.
 * When no identity is resolvable (logged out / pre-auth) the active scope is
 * `null`; foreign-purge still runs so a stale queue cannot leak forward.
 *
 * IDB safety
 * ----------
 * Every IndexedDB access is guarded by `typeof indexedDB === 'undefined'`
 * (mirrors P1b's query-persister style). When IDB is unavailable (Safari
 * private mode, locked-down enterprise, SSR, jsdom w/o fake-indexeddb) the
 * module transparently falls back to an in-memory store: it never throws, the
 * review flow is never blocked, and queued entries simply do not survive a
 * reload in that environment (acceptable per the canonical degradation policy).
 */

import {
  get as idbGet,
  set as idbSet,
  del as idbDel,
  createStore,
  type UseStore,
} from 'idb-keyval';
import { useAuthStore } from '@/stores/auth-store';

/* ---- record shape (local type — types/index.ts is F0-frozen) ------------- */

/**
 * A single queued offline review. `idempotency_key` is the client-minted dedupe
 * token the sync endpoint keys on (replay-safe). `user_id` scopes the entry for
 * cross-user safety. `reviewed_at` is the client wall-clock ISO timestamp at
 * the moment of rating (the server uses it as the authoritative review time —
 * see the endpoint contract doc).
 */
export interface QueuedReview {
  /** Client-generated UUID — the server's idempotency / dedupe key. */
  idempotency_key: string;
  /** Card the rating applies to. */
  card_id: number;
  /** FSRS rating 1..4 (1=Again, 2=Hard, 3=Good, 4=Easy). */
  rating: number;
  /** Client wall-clock ISO-8601 timestamp at rating time. */
  reviewed_at: string;
  /** Client-measured review duration (ms) — passed through to the server. */
  review_duration_ms: number | null;
  /**
   * Owning user id. `null` only when no identity was resolvable at enqueue
   * time (logged-out edge); such entries are purged on the next identity
   * change so they never sync under another user.
   */
  user_id: number | null;
}

/** Server response for a successful batch sync (see endpoint contract doc). */
export interface ReviewSyncResult {
  /** Count of review events the server applied (FSRS recomputed). */
  synced: number;
  /** Count skipped (card deleted/changed/not owned) — still removed locally. */
  skipped: number;
}

/* ---- IndexedDB-backed store (single key holds the whole queue) ----------- */

const IDB_DB_NAME = 'jarvis-review-outbox';
const IDB_STORE_NAME = 'review-outbox';
/** Single key under which the append-only queue array is stored. */
const QUEUE_KEY = 'jarvis-review-queue';

/** In-memory fallback when IndexedDB is unavailable (never throws). */
let _memQueue: QueuedReview[] | null = null;

function idbAvailable(): boolean {
  return typeof indexedDB !== 'undefined';
}

let _store: UseStore | null = null;
function store(): UseStore {
  if (_store === null) {
    _store = createStore(IDB_DB_NAME, IDB_STORE_NAME);
  }
  return _store;
}

/** Read the raw queue (all users). Never throws — empty array on any failure. */
async function readRaw(): Promise<QueuedReview[]> {
  if (!idbAvailable()) {
    return _memQueue ? [..._memQueue] : [];
  }
  try {
    const q = await idbGet<QueuedReview[]>(QUEUE_KEY, store());
    return Array.isArray(q) ? q : [];
  } catch (err) {
    console.warn('[review-outbox] read failed', err);
    return [];
  }
}

/** Persist the raw queue (all users). Never throws. */
async function writeRaw(queue: QueuedReview[]): Promise<void> {
  if (!idbAvailable()) {
    _memQueue = [...queue];
    return;
  }
  try {
    await idbSet(QUEUE_KEY, queue, store());
  } catch (err) {
    console.warn('[review-outbox] write failed', err);
  }
}

/* ---- identity (read-only; auth-store is NOT mutated) --------------------- */

/**
 * The active user id, read from the auth store's PUBLIC selector. Returns
 * `null` when nobody is authenticated (logged-out edge). This module never
 * writes to the auth store — it only reads `getUser()`.
 */
export function activeUserId(): number | null {
  try {
    return useAuthStore.getState().getUser()?.id ?? null;
  } catch {
    return null;
  }
}

/* ---- cross-user purge ---------------------------------------------------- */

/**
 * Drop every queued entry whose `user_id` differs from the currently-active
 * identity. Invoked at enqueue and drain time so a previous user's reviews can
 * never be seen or synced under the next user on a shared device. Returns the
 * filtered (active-user-only) queue. Idempotent; never throws.
 */
async function purgeForeignEntries(active: number | null): Promise<QueuedReview[]> {
  const raw = await readRaw();
  const mine = raw.filter((r) => r.user_id === active);
  if (mine.length !== raw.length) {
    await writeRaw(mine);
  }
  return mine;
}

/* ---- uuid (no new dep — crypto.randomUUID with a safe fallback) ---------- */

function newIdempotencyKey(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    /* fall through to manual */
  }
  // RFC4122-ish fallback (sufficient as a client dedupe token).
  return 'rk-' + Date.now().toString(16) + '-' + Math.random().toString(16).slice(2, 10);
}

/* ---- public API ---------------------------------------------------------- */

/**
 * Append a review to the outbox for the currently-active user. Stamps a fresh
 * idempotency key + client timestamp + the active user id. Foreign entries are
 * purged first so the queue only ever contains the active user's reviews.
 * Returns the queued record (its `idempotency_key` is the server dedupe token).
 * Never throws — a storage failure degrades to the in-memory fallback.
 */
export async function enqueueReview(
  cardId: number,
  rating: number,
  reviewDurationMs: number | null,
): Promise<QueuedReview> {
  const active = activeUserId();
  const mine = await purgeForeignEntries(active);
  const record: QueuedReview = {
    idempotency_key: newIdempotencyKey(),
    card_id: cardId,
    rating,
    reviewed_at: new Date().toISOString(),
    review_duration_ms: reviewDurationMs,
    user_id: active,
  };
  await writeRaw([...mine, record]);
  return record;
}

/**
 * The active user's queued reviews (foreign entries purged). Read-only — does
 * not drain. Used by the connectivity banner / sync status to show a count.
 */
export async function getReviewOutbox(): Promise<QueuedReview[]> {
  return purgeForeignEntries(activeUserId());
}

/**
 * Clear the ENTIRE outbox (all users). Called from a logout/cross-user-hygiene
 * path; safe to call anytime. Never throws.
 */
export async function clearReviewOutbox(): Promise<void> {
  if (!idbAvailable()) {
    _memQueue = [];
    return;
  }
  try {
    await idbDel(QUEUE_KEY, store());
  } catch (err) {
    console.warn('[review-outbox] clear failed', err);
  }
}

/**
 * Outcome of a {@link drainReviewOutbox} attempt. `status`:
 *   - `synced`   — server accepted the batch; `synced`/`skipped` populated and
 *                  those entries were removed (idempotent: re-sending the same
 *                  keys is safe by server design).
 *   - `empty`    — nothing queued for the active user (no network call made).
 *   - `deferred` — endpoint absent / network failure / non-OK; the queue is
 *                  RETAINED untouched (no data loss, caller must back off).
 */
export interface DrainOutcome {
  status: 'synced' | 'empty' | 'deferred';
  synced: number;
  skipped: number;
  /** Entries that remain queued after this attempt (active user only). */
  remaining: number;
}

/**
 * Drain the active user's outbox by POSTing the batch to the offline-sync
 * endpoint. Idempotent by design: every record carries a stable
 * `idempotency_key`, so a double-drain (e.g. two rapid online events) cannot
 * double-apply server-side and the local counts stay stable.
 *
 * Failure handling (the endpoint does NOT exist yet — functional track):
 *   - network error / 404 / 501 / any non-OK response  ⇒ status `deferred`,
 *     the queue is left INTACT. No throw, no alarming UI, no spam (the caller
 *     gates drains on the offline→online transition + the returned status).
 *
 * @param postBatch  the transport. Defaults to a credentialed POST to the
 *   contract path; injectable so tests/back-end swaps don't reach the network.
 */
export async function drainReviewOutbox(
  postBatch: (batch: QueuedReview[]) => Promise<ReviewSyncResult> = defaultPostBatch,
): Promise<DrainOutcome> {
  const active = activeUserId();
  const mine = await purgeForeignEntries(active);

  if (mine.length === 0) {
    return { status: 'empty', synced: 0, skipped: 0, remaining: 0 };
  }

  let result: ReviewSyncResult;
  try {
    result = await postBatch(mine);
  } catch (err) {
    // Endpoint absent / network failure: retain everything, surface nothing
    // alarming. The caller backs off; data is not lost.
    console.warn('[review-outbox] sync deferred (transport failed)', err);
    return { status: 'deferred', synced: 0, skipped: 0, remaining: mine.length };
  }

  // Successful response: synced + skipped entries are both removed (skipped =
  // card deleted/changed — replaying it again would never succeed). Anything
  // not covered by the response stays queued for the next attempt. We dedupe
  // by idempotency_key against the batch we actually sent so a concurrent
  // enqueue (rated while the request was in flight) is preserved.
  const sentKeys = new Set(mine.map((r) => r.idempotency_key));
  const after = await readRaw();
  const kept = after.filter(
    (r) => r.user_id !== active || !sentKeys.has(r.idempotency_key),
  );
  await writeRaw(kept);

  const remaining = kept.filter((r) => r.user_id === active).length;
  return {
    status: 'synced',
    synced: result.synced ?? 0,
    skipped: result.skipped ?? 0,
    remaining,
  };
}

/* ---- default transport --------------------------------------------------- */

/**
 * The endpoint path the client targets. The backend track implements a
 * compatible handler — see offline review sync contract.
 */
export const REVIEW_SYNC_PATH = '/api/review/sync';

/**
 * Default batch transport: a credentialed POST carrying the X-API-Key (mirrors
 * the app's auth convention) + the session cookie (`credentials: 'include'`).
 * Throws on any non-OK / network failure so {@link drainReviewOutbox} can mark
 * the attempt `deferred` and retain the queue. Body shape is the contract's
 * `{ reviews: QueuedReview[] }`.
 */
async function defaultPostBatch(batch: QueuedReview[]): Promise<ReviewSyncResult> {
  let apiKey: string;
  try {
    apiKey = useAuthStore.getState().getApiKey() ?? '';
  } catch {
    apiKey = '';
  }
  const res = await fetch(REVIEW_SYNC_PATH, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
    body: JSON.stringify({
      reviews: batch.map((r) => ({
        idempotency_key: r.idempotency_key,
        card_id: r.card_id,
        rating: r.rating,
        reviewed_at: r.reviewed_at,
        review_duration_ms: r.review_duration_ms,
      })),
    }),
  });
  if (!res.ok) {
    throw new Error(`review/sync: ${res.status}`);
  }
  return (await res.json()) as ReviewSyncResult;
}

/** Test-only: reset the in-memory fallback + cached store handle. */
export function __resetOutboxForTests(): void {
  _memQueue = null;
  _store = null;
}

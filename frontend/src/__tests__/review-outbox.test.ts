/**
 * review-outbox (Wave 3 P2) — append-only offline review outbox.
 *
 * Coverage:
 *   - enqueueReview: persists a record with idempotency_key + user_id + ts.
 *   - getReviewOutbox: returns only the active user's entries.
 *   - drainReviewOutbox: success removes sent keys + returns counts; preserves
 *     entries enqueued while in flight.
 *   - idempotent double-drain: re-sending the same keys ⇒ counts stable, no
 *     duplicate application, queue empty.
 *   - endpoint-absent (transport throws): status `deferred`, queue RETAINED,
 *     no loss, no throw.
 *   - cross-user: user B never drains / sees user A's queued reviews; foreign
 *     entries are purged on identity change.
 *   - clearReviewOutbox empties everything.
 *
 * `fake-indexeddb/auto` is installed by the shared setup so idb-keyval performs
 * a real IDB round-trip here (no mock needed for the store itself). The auth
 * store is mocked so we control the active identity WITHOUT touching the real
 * auth-store module.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { clear as idbClear } from 'idb-keyval';

// Mock the auth store's PUBLIC selector surface only — we never mutate the real
// store; the outbox reads identity via useAuthStore.getState().getUser().
let _userId: number | null = 1;
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: () => ({
      getUser: () => (_userId == null ? null : { id: _userId, email: 'x@y.z', role: 'user' }),
      getApiKey: () => 'test-key',
    }),
  },
}));

import {
  enqueueReview,
  getReviewOutbox,
  drainReviewOutbox,
  clearReviewOutbox,
  __resetOutboxForTests,
  type QueuedReview,
  type ReviewSyncResult,
} from '@/lib/review-outbox';

beforeEach(async () => {
  _userId = 1;
  __resetOutboxForTests();
  await idbClear().catch(() => {});
  await clearReviewOutbox().catch(() => {});
});

describe('enqueueReview + getReviewOutbox', () => {
  it('persists a record stamped with idempotency_key, user_id and timestamp', async () => {
    const rec = await enqueueReview(42, 3, 1500);
    expect(rec.card_id).toBe(42);
    expect(rec.rating).toBe(3);
    expect(rec.review_duration_ms).toBe(1500);
    expect(rec.user_id).toBe(1);
    expect(typeof rec.idempotency_key).toBe('string');
    expect(rec.idempotency_key.length).toBeGreaterThan(0);
    expect(() => new Date(rec.reviewed_at).toISOString()).not.toThrow();

    const queue = await getReviewOutbox();
    expect(queue).toHaveLength(1);
    expect(queue[0]?.idempotency_key).toBe(rec.idempotency_key);
  });

  it('mints a distinct idempotency_key per enqueue', async () => {
    const a = await enqueueReview(1, 1, null);
    const b = await enqueueReview(1, 1, null);
    expect(a.idempotency_key).not.toBe(b.idempotency_key);
    expect(await getReviewOutbox()).toHaveLength(2);
  });
});

describe('drainReviewOutbox — success path', () => {
  it('posts the batch, removes sent keys, returns server counts', async () => {
    await enqueueReview(10, 2, 100);
    await enqueueReview(11, 4, 200);

    const seen: QueuedReview[][] = [];
    const post = vi.fn(async (batch: QueuedReview[]): Promise<ReviewSyncResult> => {
      seen.push(batch);
      return { synced: batch.length, skipped: 0 };
    });

    const outcome = await drainReviewOutbox(post);
    expect(outcome.status).toBe('synced');
    expect(outcome.synced).toBe(2);
    expect(outcome.skipped).toBe(0);
    expect(outcome.remaining).toBe(0);
    expect(seen[0]).toHaveLength(2);
    expect(await getReviewOutbox()).toHaveLength(0);
  });

  it('removes skipped entries too (replaying them would never succeed)', async () => {
    await enqueueReview(10, 2, 100);
    await enqueueReview(11, 4, 200);
    const post = vi.fn(async (): Promise<ReviewSyncResult> => ({ synced: 1, skipped: 1 }));

    const outcome = await drainReviewOutbox(post);
    expect(outcome.status).toBe('synced');
    expect(outcome.synced).toBe(1);
    expect(outcome.skipped).toBe(1);
    expect(await getReviewOutbox()).toHaveLength(0);
  });

  it('returns status empty (no network call) when nothing is queued', async () => {
    const post = vi.fn();
    const outcome = await drainReviewOutbox(post as never);
    expect(outcome.status).toBe('empty');
    expect(post).not.toHaveBeenCalled();
  });
});

describe('idempotent double-drain', () => {
  it('re-sending the same keys keeps counts stable and never duplicates', async () => {
    await enqueueReview(7, 3, 50);
    await enqueueReview(8, 1, 60);

    // Server is idempotent by contract: a re-seen key still counts as synced.
    const post = vi.fn(
      async (batch: QueuedReview[]): Promise<ReviewSyncResult> => ({
        synced: batch.length,
        skipped: 0,
      }),
    );

    const first = await drainReviewOutbox(post);
    expect(first.status).toBe('synced');
    expect(first.synced).toBe(2);
    expect(await getReviewOutbox()).toHaveLength(0);

    // Second drain: queue already empty ⇒ no duplicate POST, counts stable.
    const second = await drainReviewOutbox(post);
    expect(second.status).toBe('empty');
    expect(post).toHaveBeenCalledTimes(1);
    expect(await getReviewOutbox()).toHaveLength(0);
  });

  it('preserves an entry enqueued while a drain is in flight', async () => {
    await enqueueReview(1, 3, null);

    const post = vi.fn(async (batch: QueuedReview[]): Promise<ReviewSyncResult> => {
      // Simulate a rating made by the user while the request is in flight.
      await enqueueReview(2, 4, null);
      return { synced: batch.length, skipped: 0 };
    });

    const outcome = await drainReviewOutbox(post);
    expect(outcome.status).toBe('synced');
    expect(outcome.synced).toBe(1);
    const remaining = await getReviewOutbox();
    expect(remaining).toHaveLength(1);
    expect(remaining[0]?.card_id).toBe(2);
  });
});

describe('endpoint absent / transport failure', () => {
  it('retains the queue, does not throw, reports deferred (404/network)', async () => {
    await enqueueReview(99, 2, 10);
    await enqueueReview(100, 3, 20);

    const post = vi.fn(async (): Promise<ReviewSyncResult> => {
      throw new Error('review/sync: 404');
    });

    const outcome = await drainReviewOutbox(post);
    expect(outcome.status).toBe('deferred');
    expect(outcome.synced).toBe(0);
    expect(outcome.skipped).toBe(0);
    expect(outcome.remaining).toBe(2);
    // No data loss — both entries still queued for the next attempt.
    expect(await getReviewOutbox()).toHaveLength(2);
  });
});

describe('cross-user safety (no auth-store mutation)', () => {
  it('user B does not see or drain user A queued reviews', async () => {
    _userId = 1;
    await enqueueReview(500, 3, 100); // user A queues a review

    // User B logs in on the same device.
    _userId = 2;
    expect(await getReviewOutbox()).toHaveLength(0); // B sees nothing

    const post = vi.fn(
      async (batch: QueuedReview[]): Promise<ReviewSyncResult> => ({
        synced: batch.length,
        skipped: 0,
      }),
    );
    const outcome = await drainReviewOutbox(post);
    // B's drain finds nothing of B's; A's entry was purged as foreign.
    expect(outcome.status).toBe('empty');
    expect(post).not.toHaveBeenCalled();

    // Back to A: A's review was purged when B became active (cross-user hygiene).
    _userId = 1;
    expect(await getReviewOutbox()).toHaveLength(0);
  });

  it('the drained batch only ever contains the active user entries', async () => {
    _userId = 1;
    await enqueueReview(1, 1, null);
    await enqueueReview(2, 2, null);
    _userId = 1;

    let captured: QueuedReview[] = [];
    const post = vi.fn(async (batch: QueuedReview[]): Promise<ReviewSyncResult> => {
      captured = batch;
      return { synced: batch.length, skipped: 0 };
    });
    await drainReviewOutbox(post);
    expect(captured.every((r) => r.user_id === 1)).toBe(true);
  });
});

describe('clearReviewOutbox', () => {
  it('empties the entire outbox', async () => {
    await enqueueReview(1, 1, null);
    await enqueueReview(2, 2, null);
    await clearReviewOutbox();
    expect(await getReviewOutbox()).toHaveLength(0);
  });
});

/**
 * useOfflineReviewQueue — the P2 offline-review seam implementation.
 *
 * Wave 3 P2. Consumed ONLY by LearningCardsPage via the F8 single-boundary
 * `submitReviewFn` prop of <ReviewMode> (commented "P2 OFFLINE SEAM"). Nothing
 * else changes — ReviewMode is not rearchitected.
 *
 * Contract references:
 *   - internal design spec (archived)
 *     §Offline → "P2 (offline review + sync) — the flow this spec owns".
 *   - internal design spec (archived)
 *     "Offline / PWA contract — CANONICAL": reconcile = a SINGLE toast
 *     "N synced, M skipped" (no merge UI).
 *   - internal design spec (archived)
 *     (the wire contract handed to the functional/backend track).
 *
 * Behaviour
 * ---------
 *   - ONLINE  → returns the real `submitReview` UNCHANGED. The live per-card
 *     endpoint is called exactly as today; FSRS/online semantics untouched.
 *   - OFFLINE → returns a fn that appends the rating to the IndexedDB outbox
 *     (review-outbox), resolves immediately so the session UI advances
 *     optimistically, and never blocks the review flow.
 *   - offline → online TRANSITION → drains the outbox once via the contract
 *     endpoint. Endpoint absent / network failure ⇒ queue retained, backed
 *     off, nothing alarming surfaced. A successful response ⇒ a SINGLE toast
 *     "N synced, M skipped" and the synced+skipped entries are removed
 *     (idempotent: re-sending the same keys is server-safe by design).
 */

import { useCallback, useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import { submitReview } from '@/lib/api';
import { useOnlineStatus } from '@/hooks/use-online-status';
import {
  enqueueReview,
  drainReviewOutbox,
  getReviewOutbox,
} from '@/lib/review-outbox';

/** Backoff window between deferred drain attempts (endpoint not live yet). */
const DRAIN_BACKOFF_MS = 60_000;

export interface UseOfflineReviewQueue {
  /**
   * Drop-in replacement for the seam's `submitReviewFn`. Identical signature to
   * the online `submitReview` so the swap is a one-liner at the call site.
   * Online: delegates to the real endpoint. Offline: enqueues + resolves so the
   * optimistic session advance proceeds.
   */
  submitReviewFn: (cardId: number, rating: number, durationMs: number) => Promise<unknown>;
}

export function useOfflineReviewQueue(): UseOfflineReviewQueue {
  const { online } = useOnlineStatus();
  const queryClient = useQueryClient();

  // Track the previous connectivity so we only drain on a genuine
  // offline→online EDGE (not on every render while online).
  const prevOnline = useRef<boolean>(online);
  // Guards: never run two drains at once; back off when the endpoint is absent
  // so we don't spam the network on a flapping connection.
  const draining = useRef<boolean>(false);
  const nextDrainAllowedAt = useRef<number>(0);

  const drain = useCallback(async () => {
    if (draining.current) return;
    if (Date.now() < nextDrainAllowedAt.current) return;

    // Skip the network entirely when nothing is queued for the active user.
    const pending = await getReviewOutbox();
    if (pending.length === 0) return;

    draining.current = true;
    try {
      const outcome = await drainReviewOutbox();
      if (outcome.status === 'synced') {
        // SINGLE reconcile toast — no merge UI (canonical contract).
        toast.success(`${outcome.synced} synced, ${outcome.skipped} skipped`);
        // Server recomputed FSRS — refresh the read surfaces.
        queryClient.invalidateQueries({ queryKey: QUERY_KEYS.cards.stats() });
        queryClient.invalidateQueries({ queryKey: QUERY_KEYS.reviewQueue.next() });
      } else if (outcome.status === 'deferred') {
        // Endpoint absent / transport failed: retain the queue, back off,
        // surface nothing alarming. It will retry on the next online edge
        // after the backoff window.
        nextDrainAllowedAt.current = Date.now() + DRAIN_BACKOFF_MS;
      }
    } finally {
      draining.current = false;
    }
  }, [queryClient]);

  // Drain on the offline→online transition only.
  useEffect(() => {
    const wasOffline = prevOnline.current === false;
    prevOnline.current = online;
    if (online && wasOffline) {
      void drain();
    }
  }, [online, drain]);

  // Also attempt a drain on mount when we come up already online with a queue
  // left over from a previous session (e.g. tab was closed while offline).
  useEffect(() => {
    if (online) {
      void drain();
    }
    // Intentionally mount-only: subsequent drains are edge-driven above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitReviewFn = useCallback(
    (cardId: number, rating: number, durationMs: number): Promise<unknown> => {
      if (online) {
        // ONLINE: behave EXACTLY as today — call the real endpoint. No outbox,
        // no FSRS change here. Online behaviour is provably unchanged.
        return submitReview(cardId, rating, durationMs);
      }
      // OFFLINE: append to the outbox and resolve immediately so the session
      // optimistically advances. Never block the review flow offline.
      return enqueueReview(cardId, rating, durationMs);
    },
    [online],
  );

  return { submitReviewFn };
}

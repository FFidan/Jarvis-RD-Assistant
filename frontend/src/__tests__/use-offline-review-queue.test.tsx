/**
 * useOfflineReviewQueue — the offline-review seam hook.
 *
 * Coverage:
 *   - ONLINE: returned submitReviewFn calls the REAL submitReview (api), NOT
 *     the outbox — online behaviour provably unchanged.
 *   - OFFLINE: returned submitReviewFn enqueues to the outbox and resolves
 *     (optimistic advance), real submitReview NOT called.
 *   - offline→online transition drains the outbox and shows the SINGLE
 *     reconcile toast "N synced, M skipped".
 *   - endpoint-absent on reconnect: no toast, no throw, queue retained.
 *
 * idb-keyval is mocked with a minimal in-memory map (deterministic, no real
 * IDB needed for the hook-level assertions). api.submitReview, the online
 * status hook, sonner toast and the outbox transport are all mocked so the
 * test is hermetic and never touches the network.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

// --- idb-keyval: tiny in-memory mock (single-key queue) --------------------
const _idb = new Map<string, unknown>();
vi.mock('idb-keyval', () => ({
  get: vi.fn(async (k: string) => _idb.get(k)),
  set: vi.fn(async (k: string, v: unknown) => {
    _idb.set(k, v);
  }),
  del: vi.fn(async (k: string) => {
    _idb.delete(k);
  }),
  createStore: vi.fn(() => ({})),
}));

// --- auth store public selector (read-only; real store untouched) ----------
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: () => ({
      getUser: () => ({ id: 1, email: 'a@b.c', role: 'user' }),
      getApiKey: () => 'k',
    }),
  },
}));

// --- online status (controllable) ------------------------------------------
let _online = true;
vi.mock('@/hooks/use-online-status', () => ({
  useOnlineStatus: () => ({ online: _online }),
}));

// --- real api submitReview (the online path we must NOT change) ------------
const submitReviewSpy = vi.fn(async (..._a: unknown[]) => ({ ok: true }));
vi.mock('@/lib/api', () => ({
  submitReview: (cardId: number, rating: number, durationMs?: number) =>
    submitReviewSpy(cardId, rating, durationMs),
}));

// --- sonner toast ----------------------------------------------------------
// Shared sonner mock; the success member is additionally routed through the
// toastSuccess spy the assertions in this file use.
const toastSuccess = vi.fn();
vi.mock('sonner', async () => {
  const { createSonnerMock } = await import('@/__tests__/fixtures/sonner-mock');
  const mock = createSonnerMock();
  mock.toast.success = vi.fn((...a: unknown[]) => toastSuccess(...a));
  return mock;
});

import { useOfflineReviewQueue } from '@/components/cards/use-offline-review-queue';
import { getReviewOutbox, __resetOutboxForTests } from '@/lib/review-outbox';
import { createTestQueryClient } from '@/__tests__/test-utils';

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = createTestQueryClient();
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  _idb.clear();
  _online = true;
  submitReviewSpy.mockClear();
  toastSuccess.mockClear();
  __resetOutboxForTests();
});

describe('ONLINE — behaviour unchanged', () => {
  it('delegates to the real submitReview and does NOT enqueue', async () => {
    _online = true;
    const { result } = renderHook(() => useOfflineReviewQueue(), { wrapper });

    await act(async () => {
      await result.current.submitReviewFn(7, 3, 1234);
    });

    expect(submitReviewSpy).toHaveBeenCalledWith(7, 3, 1234);
    expect(await getReviewOutbox()).toHaveLength(0);
  });
});

describe('OFFLINE — enqueue + optimistic resolve', () => {
  it('enqueues to the outbox and does NOT call the real endpoint', async () => {
    _online = false;
    const { result } = renderHook(() => useOfflineReviewQueue(), { wrapper });

    await act(async () => {
      await result.current.submitReviewFn(9, 4, 555);
    });

    expect(submitReviewSpy).not.toHaveBeenCalled();
    const q = await getReviewOutbox();
    expect(q).toHaveLength(1);
    expect(q[0]?.card_id).toBe(9);
    expect(q[0]?.rating).toBe(4);
  });
});

describe('offline → online — drain + single reconcile toast', () => {
  it('drains the queue and shows "N synced, M skipped" once', async () => {
    // Queue a review while offline.
    _online = false;
    const { result, rerender } = renderHook(() => useOfflineReviewQueue(), {
      wrapper,
    });
    await act(async () => {
      await result.current.submitReviewFn(3, 2, 10);
    });
    expect(await getReviewOutbox()).toHaveLength(1);

    // Stub the network so the default transport reports a successful sync.
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ synced: 1, skipped: 0 }),
    } as Response);

    // Transition offline → online.
    _online = true;
    await act(async () => {
      rerender();
    });

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1));
    expect(toastSuccess).toHaveBeenCalledWith('1 synced, 0 skipped');
    await waitFor(async () => expect(await getReviewOutbox()).toHaveLength(0));
    fetchSpy.mockRestore();
  });
});

describe('offline → online — endpoint absent', () => {
  it('does not toast, does not throw, retains the queue', async () => {
    _online = false;
    const { result, rerender } = renderHook(() => useOfflineReviewQueue(), {
      wrapper,
    });
    await act(async () => {
      await result.current.submitReviewFn(5, 1, 99);
    });

    // Endpoint not deployed → 404.
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({}),
    } as Response);

    _online = true;
    await act(async () => {
      rerender();
    });

    // Give the async drain a tick; nothing alarming happens.
    await new Promise((r) => setTimeout(r, 20));
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(await getReviewOutbox()).toHaveLength(1); // retained, no loss
    fetchSpy.mockRestore();
  });
});

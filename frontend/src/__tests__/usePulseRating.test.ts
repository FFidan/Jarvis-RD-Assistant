/**
 * Mutation lifecycle tests for usePulseRating (FE-UIA-05).
 *
 * Verifies the three TanStack Query lifecycle handlers that would not be
 * caught by any other test:
 *   - onMutate  : optimistic cache write (card.user_state → 'to_read')
 *   - onError   : cache revert + toast.error on failure
 *   - onSettled : pulse-today query invalidation
 *
 * Revert-proof: removing any of the three handlers causes at least one test
 * here to fail (see inline reasoning comments).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import type { PulseDeck } from '@/types';

// ---------------------------------------------------------------------------
// Mock ratePulseCard before the hook is imported
// ---------------------------------------------------------------------------
vi.mock('@/lib/api', () => ({
  ratePulseCard: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

const { ratePulseCard } = await import('@/lib/api');
const { toast } = await import('sonner');
const { usePulseRating } = await import('@/hooks/usePulseRating');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a minimal PulseDeck with one seeded card. */
function makeDeck(paperId: number, userState: string | null = 'inbox'): PulseDeck {
  return {
    deck_id: 1,
    deck_date: '2026-06-05',
    card_count: 1,
    generated_at: '2026-06-05T08:00:00Z',
    stats: {},
    cards: [
      {
        card_id: 10,
        paper_id: paperId,
        paper_title: 'Test Paper',
        paper_authors: ['Alice'],
        paper_url: null,
        rank: 1,
        score: 0.9,
        llm_relevance: 8,
        llm_novelty: 7,
        reasoning: 'test reasoning',
        reasoning_verified: null,
        reasoning_confidence: null,
        signals: {},
        user_state: userState as 'inbox' | 'to_read' | 'reading' | 'done' | 'trash' | null,
      },
    ],
  };
}

/** Create a fresh QueryClient with retries disabled so mutations fail fast. */
function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

/** Returns a renderHook wrapper that provides the given QueryClient. */
function makeWrapper(qc: QueryClient) {
  const Wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children);
  Wrapper.displayName = 'QueryWrapper';
  return Wrapper;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('usePulseRating — onMutate optimistic update', () => {
  let qc: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    qc = makeQueryClient();
  });

  it('synchronously sets user_state to "to_read" for the rated card before the mutationFn resolves', async () => {
    // Arrange: seed the pulse-today cache
    const PAPER_ID = 101;
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), makeDeck(PAPER_ID, 'inbox'));

    // ratePulseCard hangs — never resolves during this test, ensuring we
    // inspect the cache *before* the mutation settles
    let resolveHang!: (v: unknown) => void;
    (ratePulseCard as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise((res) => { resolveHang = res; }),
    );

    const { result } = renderHook(() => usePulseRating(), {
      wrapper: makeWrapper(qc),
    });

    // Act: fire the mutation — does NOT await so we inspect mid-flight state
    act(() => {
      result.current.mutate({ paperId: PAPER_ID, rating: 'save' });
    });

    // Assert: onMutate must have run synchronously-ish (within microtasks)
    // If onMutate is removed, the card remains user_state='inbox' here.
    await waitFor(() => {
      const deck = qc.getQueryData<PulseDeck>(QUERY_KEYS.pulse.today());
      expect(deck?.cards[0]?.user_state).toBe('to_read');
    });

    // Cleanup: let the mutation settle so the QueryClient doesn't leak
    resolveHang(undefined);
  });

  it('does NOT mutate the cache for ratings other than "save"', async () => {
    const PAPER_ID = 102;
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), makeDeck(PAPER_ID, 'inbox'));

    (ratePulseCard as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    const { result } = renderHook(() => usePulseRating(), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      result.current.mutate({ paperId: PAPER_ID, rating: 'up' });
    });

    await waitFor(() => result.current.isIdle || result.current.isSuccess);

    // user_state must remain unchanged — onMutate early-returns for non-save
    const deck = qc.getQueryData<PulseDeck>(QUERY_KEYS.pulse.today());
    expect(deck?.cards[0]?.user_state).toBe('inbox');
  });
});

describe('usePulseRating — onError cache revert', () => {
  let qc: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    qc = makeQueryClient();
  });

  it('reverts the optimistic update when the mutationFn rejects', async () => {
    // Arrange: seed deck and make ratePulseCard fail
    const PAPER_ID = 201;
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), makeDeck(PAPER_ID, 'inbox'));

    (ratePulseCard as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('server error'),
    );

    const { result } = renderHook(() => usePulseRating(), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      result.current.mutate({ paperId: PAPER_ID, rating: 'save' });
    });

    // Wait for the mutation to fail
    await waitFor(() => result.current.isError);

    // onError must have restored the original deck.
    // If onError is removed, user_state stays as 'to_read' after the failure.
    const deck = qc.getQueryData<PulseDeck>(QUERY_KEYS.pulse.today());
    expect(deck?.cards[0]?.user_state).toBe('inbox');
  });

  it('calls toast.error when the mutationFn rejects', async () => {
    const PAPER_ID = 202;
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), makeDeck(PAPER_ID, 'inbox'));

    (ratePulseCard as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('network timeout'),
    );

    const { result } = renderHook(() => usePulseRating(), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      result.current.mutate({ paperId: PAPER_ID, rating: 'save' });
    });

    await waitFor(() => result.current.isError);

    // If onError is removed, toast.error is never called.
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('network timeout'));
  });
});

describe('usePulseRating — onSettled query invalidation', () => {
  let qc: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    qc = makeQueryClient();
  });

  it('marks the pulse-today query stale (invalidates) after a successful mutation', async () => {
    const PAPER_ID = 301;
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), makeDeck(PAPER_ID));

    (ratePulseCard as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    // Spy on invalidateQueries to assert it's called with the correct key
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    const { result } = renderHook(() => usePulseRating(), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      result.current.mutate({ paperId: PAPER_ID, rating: 'up' });
    });

    await waitFor(() => result.current.isSuccess || result.current.isError);

    // onSettled must have called invalidateQueries with pulse.today() key.
    // If onSettled is removed, this spy is never called.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: QUERY_KEYS.pulse.today() }),
    );
  });

  it('invalidates the pulse-today query even after a failed mutation (onSettled always runs)', async () => {
    const PAPER_ID = 302;
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), makeDeck(PAPER_ID));

    (ratePulseCard as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('boom'),
    );

    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    const { result } = renderHook(() => usePulseRating(), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      result.current.mutate({ paperId: PAPER_ID, rating: 'down' });
    });

    await waitFor(() => result.current.isError);

    // onSettled runs on both success AND error paths.
    // If onSettled is removed, this assertion fails even for the error path.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: QUERY_KEYS.pulse.today() }),
    );
  });
});

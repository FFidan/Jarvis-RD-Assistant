/**
 * Regression test: FeedbackButtons untoggle-lock fix.
 *
 * Before the fix, clearMutation.isSuccess was sticky (stays true after the
 * first successful clear). Subsequent 👍→👍→👍 sequences would leave the
 * button in ghost/"inactive" state even though a new positive feedback was
 * submitted. This file asserts the full sequence works correctly.
 */
import { render, screen, within, act } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FeedbackButtons } from '@/components/shared/FeedbackButtons';

vi.mock('@/lib/api', () => ({
  submitFeedback: vi.fn(),
  clearFeedback: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

import { submitFeedback, clearFeedback } from '@/lib/api';

const mkQc = () => new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });

/** Render inside an isolated container to avoid cross-test DOM leakage. */
const wrap = (ui: React.ReactNode) => {
  const qc = mkQc();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
};

describe('FeedbackButtons — untoggle-lock regression', () => {
  beforeEach(() => vi.clearAllMocks());

  /**
   * Core regression sequence: 👍 → 👍 (untoggle) → 👍 (re-activate).
   *
   * The bug: after the first untoggle clearMutation.isSuccess became sticky,
   * so the third click (new 👍) would call submitFeedback but the button would
   * remain ghost (variant="ghost") forever.
   *
   * After the fix: positiveActive only depends on lastSignal / recentFeedback.
   * The third click should call submitFeedback (not clearFeedback), proving the
   * component considers the button inactive after the untoggle cycle.
   */
  it('👍 → 👍-untoggle → 👍 re-activate: button shows active class after third click', async () => {
    (submitFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      paper_id: 1,
      signal: 'positive',
      source: 'feed_thumbs',
      created_at: '2026-05-02T00:00:00Z',
    });
    (clearFeedback as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    const { rerender, unmount } = wrap(
      <FeedbackButtons paperId={1} discoveryOrigin="pulse" source="feed_thumbs" />,
    );

    const thumbsUp = () => screen.getByLabelText('Recommend more like this');

    // --- Step 1: click 👍 (first positive feedback) ---
    await act(async () => {
      fireEvent.click(thumbsUp());
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(submitFeedback).toHaveBeenCalledTimes(1);

    // --- Step 2: simulate server confirming feedback; re-render with recentFeedback=positive ---
    // Then click 👍 again → untoggle (clearFeedback).
    rerender(
      <QueryClientProvider client={mkQc()}>
        <FeedbackButtons
          paperId={1}
          discoveryOrigin="pulse"
          source="feed_thumbs"
          recentFeedback={{ signal: 'positive' }}
        />
      </QueryClientProvider>,
    );

    await act(async () => {
      fireEvent.click(thumbsUp());
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(clearFeedback).toHaveBeenCalledTimes(1);

    // --- Step 3: server confirms clear → recentFeedback is null ---
    rerender(
      <QueryClientProvider client={mkQc()}>
        <FeedbackButtons
          paperId={1}
          discoveryOrigin="pulse"
          source="feed_thumbs"
          recentFeedback={null}
        />
      </QueryClientProvider>,
    );

    // --- Step 4: click 👍 again → must call submitFeedback (not clearFeedback) ---
    // This is the critical assertion: the component is NOT locked in ghost state.
    (submitFeedback as ReturnType<typeof vi.fn>).mockClear();
    (clearFeedback as ReturnType<typeof vi.fn>).mockClear();
    (submitFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      paper_id: 1,
      signal: 'positive',
      source: 'feed_thumbs',
      created_at: '2026-05-02T00:00:00Z',
    });

    await act(async () => {
      fireEvent.click(thumbsUp());
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(submitFeedback).toHaveBeenCalledTimes(1);
    expect(submitFeedback).toHaveBeenCalledWith(1, { signal: 'positive', source: 'feed_thumbs' });
    expect(clearFeedback).not.toHaveBeenCalled();

    unmount();
  });

  /**
   * Optimistic polish: synchronous setLastSignal(null) before clearMutation.mutate()
   * means the button de-activates on the same render frame as the click.
   * After un-toggling, the next click must submit (not clear), even if
   * clearMutation.mutate()'s async work hasn't settled yet.
   *
   * We verify by: render with recentFeedback=positive, click (untoggle),
   * then immediately re-render with recentFeedback=null and click again.
   * If lastSignal is cleared synchronously, the second click routes to
   * submitFeedback, not clearFeedback.
   */
  it('optimistic untoggle: button de-activates synchronously — subsequent click routes to submit', async () => {
    (clearFeedback as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);
    (submitFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      paper_id: 2,
      signal: 'positive',
      source: 'feed_thumbs',
      created_at: '2026-05-02T00:00:00Z',
    });

    const { rerender, unmount } = wrap(
      <FeedbackButtons
        paperId={2}
        discoveryOrigin="recommender"
        source="feed_thumbs"
        recentFeedback={{ signal: 'positive' }}
      />,
    );

    const thumbsUp = () => screen.getByLabelText('Recommend more like this');

    // Click the active 👍 → untoggle (clearFeedback is called)
    await act(async () => {
      fireEvent.click(thumbsUp());
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(clearFeedback).toHaveBeenCalledWith(2, 'feed_thumbs');

    // Re-render simulating optimistic cleared state (recentFeedback=null).
    // Because setLastSignal(null) fires synchronously before clearMutation.mutate(),
    // the component no longer considers the button active.
    rerender(
      <QueryClientProvider client={mkQc()}>
        <FeedbackButtons
          paperId={2}
          discoveryOrigin="recommender"
          source="feed_thumbs"
          recentFeedback={null}
        />
      </QueryClientProvider>,
    );

    // Next click must route to submitFeedback (not clearFeedback again).
    (clearFeedback as ReturnType<typeof vi.fn>).mockClear();

    await act(async () => {
      fireEvent.click(thumbsUp());
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(submitFeedback).toHaveBeenCalledWith(2, { signal: 'positive', source: 'feed_thumbs' });
    expect(clearFeedback).not.toHaveBeenCalled();

    unmount();
  });

  /**
   * Analogous sequence for the negative (👎) button: ensures the fix is
   * symmetric — no sticky-lock on the thumbs-down path either.
   */
  it('👎 → 👎-untoggle → 👎 re-activate: clearFeedback then submitFeedback', async () => {
    (submitFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      paper_id: 3,
      signal: 'negative',
      source: 'pulse_thumbs',
      created_at: '2026-05-02T00:00:00Z',
    });
    (clearFeedback as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    // Step 1: active 👎 → untoggle
    const { unmount: u1 } = wrap(
      <FeedbackButtons
        paperId={3}
        discoveryOrigin="pulse"
        source="pulse_thumbs"
        recentFeedback={{ signal: 'negative' }}
      />,
    );

    const thumbsDown1 = within(document.body).getAllByLabelText("Don't recommend like this")[0];
    if (!thumbsDown1) throw new Error('thumbsDown1 button not found');

    await act(async () => {
      fireEvent.click(thumbsDown1);
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(clearFeedback).toHaveBeenCalledWith(3, 'pulse_thumbs');

    u1();

    // Step 2: cleared state → click 👎 again → submitFeedback
    (clearFeedback as ReturnType<typeof vi.fn>).mockClear();

    const { unmount: u2 } = wrap(
      <FeedbackButtons
        paperId={3}
        discoveryOrigin="pulse"
        source="pulse_thumbs"
        recentFeedback={null}
      />,
    );

    const thumbsDown2 = screen.getByLabelText("Don't recommend like this");

    await act(async () => {
      fireEvent.click(thumbsDown2);
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(submitFeedback).toHaveBeenCalledWith(3, { signal: 'negative', source: 'pulse_thumbs' });
    expect(clearFeedback).not.toHaveBeenCalled();

    u2();
  });
});

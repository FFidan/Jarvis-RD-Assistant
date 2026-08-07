import { render, screen } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import { FeedbackButtons } from '@/components/shared/FeedbackButtons';

vi.mock('@/lib/api', () => ({
  submitFeedback: vi.fn(),
  clearFeedback: vi.fn(),
}));

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

import { submitFeedback, clearFeedback } from '@/lib/api';
import { toast } from 'sonner';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const wrap = (ui: React.ReactNode) => {
  const qc = createTestQueryClient({ defaultOptions: { mutations: { retry: false } } });
  return renderWithProviders(
    ui,
    { queryClient: qc },
  );
};

describe('FeedbackButtons', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders nothing when discoveryOrigin === user_initiated', () => {
    const { container } = wrap(
      <FeedbackButtons paperId={1} discoveryOrigin="user_initiated" source="feed_thumbs" />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders both buttons when discoveryOrigin === pulse', () => {
    wrap(<FeedbackButtons paperId={1} discoveryOrigin="pulse" source="pulse_thumbs" />);
    expect(screen.getByLabelText('Recommend more like this')).toBeInTheDocument();
    expect(screen.getByLabelText("Don't recommend like this")).toBeInTheDocument();
  });

  it('clicking thumbs-up calls submitFeedback with signal=positive', async () => {
    (submitFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      paper_id: 42,
      signal: 'positive',
      source: 'feed_thumbs',
      created_at: '2026-04-30T00:00:00Z',
    });
    wrap(<FeedbackButtons paperId={42} discoveryOrigin="recommender" source="feed_thumbs" />);
    fireEvent.click(screen.getByLabelText('Recommend more like this'));
    // Allow microtask queue to flush
    await new Promise((r) => setTimeout(r, 0));
    expect(submitFeedback).toHaveBeenCalledWith(42, { signal: 'positive', source: 'feed_thumbs' });
  });

  it('onError fires toast.error on mutation failure', async () => {
    (submitFeedback as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    wrap(<FeedbackButtons paperId={1} discoveryOrigin="pulse" source="pulse_thumbs" />);
    fireEvent.click(screen.getByLabelText('Recommend more like this'));
    await new Promise((r) => setTimeout(r, 50));
    expect(toast.error).toHaveBeenCalled();
  });

  it('renders for recommender origin', () => {
    wrap(<FeedbackButtons paperId={1} discoveryOrigin="recommender" source="feed_thumbs" />);
    expect(screen.getByLabelText('Recommend more like this')).toBeInTheDocument();
    expect(screen.getByLabelText("Don't recommend like this")).toBeInTheDocument();
  });

  it('renders for citation_batch origin', () => {
    wrap(<FeedbackButtons paperId={1} discoveryOrigin="citation_batch" source="feed_thumbs" />);
    expect(screen.getByLabelText('Recommend more like this')).toBeInTheDocument();
    expect(screen.getByLabelText("Don't recommend like this")).toBeInTheDocument();
  });

  it('clicking thumbs-down calls submitFeedback with signal=negative', async () => {
    (submitFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      paper_id: 7,
      signal: 'negative',
      source: 'feed_thumbs',
      created_at: '2026-05-01T00:00:00Z',
    });
    wrap(<FeedbackButtons paperId={7} discoveryOrigin="pulse" source="feed_thumbs" />);
    fireEvent.click(screen.getByLabelText("Don't recommend like this"));
    await new Promise((r) => setTimeout(r, 0));
    expect(submitFeedback).toHaveBeenCalledWith(7, { signal: 'negative', source: 'feed_thumbs' });
  });

  describe('B.1 — untoggle (click active thumb again → clearFeedback)', () => {
    it('clicking active 👍 (recentFeedback=positive) calls clearFeedback and NOT submitFeedback', async () => {
      (clearFeedback as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);
      wrap(
        <FeedbackButtons
          paperId={99}
          discoveryOrigin="pulse"
          source="pulse_thumbs"
          recentFeedback={{ signal: 'positive' }}
        />,
      );
      fireEvent.click(screen.getByLabelText('Recommend more like this'));
      await new Promise((r) => setTimeout(r, 0));
      expect(clearFeedback).toHaveBeenCalledWith(99, 'pulse_thumbs');
      expect(submitFeedback).not.toHaveBeenCalled();
    });

    it('clicking active 👎 (recentFeedback=negative) calls clearFeedback and NOT submitFeedback', async () => {
      (clearFeedback as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);
      wrap(
        <FeedbackButtons
          paperId={88}
          discoveryOrigin="recommender"
          source="feed_thumbs"
          recentFeedback={{ signal: 'negative' }}
        />,
      );
      fireEvent.click(screen.getByLabelText("Don't recommend like this"));
      await new Promise((r) => setTimeout(r, 0));
      expect(clearFeedback).toHaveBeenCalledWith(88, 'feed_thumbs');
      expect(submitFeedback).not.toHaveBeenCalled();
    });

    it('switching 👍→👎 calls submitFeedback (UPSERT) not clearFeedback', async () => {
      (submitFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
        paper_id: 77,
        signal: 'negative',
        source: 'feed_thumbs',
        created_at: '2026-05-01T00:00:00Z',
      });
      wrap(
        <FeedbackButtons
          paperId={77}
          discoveryOrigin="pulse"
          source="feed_thumbs"
          recentFeedback={{ signal: 'positive' }}
        />,
      );
      // Clicking the opposite (👎 while 👍 is active) → submitFeedback
      fireEvent.click(screen.getByLabelText("Don't recommend like this"));
      await new Promise((r) => setTimeout(r, 0));
      expect(submitFeedback).toHaveBeenCalledWith(77, { signal: 'negative', source: 'feed_thumbs' });
      expect(clearFeedback).not.toHaveBeenCalled();
    });
  });

  it('does not violate rules-of-hooks when discoveryOrigin toggles across renders', () => {
    const qc = createTestQueryClient({ defaultOptions: { mutations: { retry: false } } });
    const Wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { rerender } = render(
      <FeedbackButtons paperId={1} discoveryOrigin="pulse" source="pulse_thumbs" />,
      { wrapper: Wrapper },
    );
    expect(() =>
      rerender(<FeedbackButtons paperId={1} discoveryOrigin="user_initiated" source="pulse_thumbs" />),
    ).not.toThrow();
  });
});

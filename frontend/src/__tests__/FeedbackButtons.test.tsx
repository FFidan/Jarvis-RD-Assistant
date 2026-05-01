import { render, screen } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FeedbackButtons } from '@/components/shared/FeedbackButtons';

vi.mock('@/lib/api', () => ({
  submitFeedback: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

import { submitFeedback } from '@/lib/api';
import { toast } from 'sonner';

const wrap = (ui: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
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
});

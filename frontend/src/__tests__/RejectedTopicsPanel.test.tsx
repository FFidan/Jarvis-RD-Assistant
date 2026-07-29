import { screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { RejectedTopicsPanel } from '@/components/settings/RejectedTopicsPanel';

vi.mock('@/lib/api', () => ({
  fetchRecommendationFeedback: vi.fn(),
  deleteRecommendationFeedback: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import { fetchRecommendationFeedback, deleteRecommendationFeedback } from '@/lib/api';
import { toast } from 'sonner';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const wrap = (ui: React.ReactNode) => {
  const qc = createTestQueryClient();
  return renderWithProviders(
    ui,
    { queryClient: qc },
  );
};

describe('RejectedTopicsPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders empty state when 0 negatives', async () => {
    (fetchRecommendationFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [], total: 0 });
    wrap(<RejectedTopicsPanel />);
    await waitFor(() => expect(screen.getByText(/No topics rejected yet/i)).toBeInTheDocument());
  });

  it('groups items by topic_id and renders sorted by count', async () => {
    (fetchRecommendationFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        { paper_id: 1, title: 'P1', signal: 'negative', source: 'pulse_thumbs', reason: null, topic_id: 5, topic_name: 'NLP', created_at: '...' },
        { paper_id: 2, title: 'P2', signal: 'negative', source: 'pulse_thumbs', reason: null, topic_id: 5, topic_name: 'NLP', created_at: '...' },
        { paper_id: 3, title: 'P3', signal: 'negative', source: 'pulse_thumbs', reason: null, topic_id: 7, topic_name: 'CV', created_at: '...' },
        { paper_id: 4, title: 'P4', signal: 'positive', source: 'pulse_thumbs', reason: null, topic_id: 5, topic_name: 'NLP', created_at: '...' },  // ignored: positive
        { paper_id: 5, title: 'P5', signal: 'negative', source: 'pulse_thumbs', reason: null, topic_id: null, topic_name: null, created_at: '...' },  // ignored: null topic
      ],
      total: 5,
    });
    wrap(<RejectedTopicsPanel />);
    await waitFor(() => expect(screen.getByText('NLP')).toBeInTheDocument());
    expect(screen.getByText('CV')).toBeInTheDocument();
    expect(screen.getByText(/2 papers rejected/i)).toBeInTheDocument();
    expect(screen.getByText(/1 paper rejected/i)).toBeInTheDocument();
  });

  it('reset button calls deleteRecommendationFeedback with topic_id', async () => {
    (fetchRecommendationFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [{ paper_id: 1, title: 'P', signal: 'negative', source: 'pulse_thumbs', reason: null, topic_id: 5, topic_name: 'NLP', created_at: '...' }],
      total: 1,
    });
    (deleteRecommendationFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({ deleted: 1, topic_id: 5 });
    wrap(<RejectedTopicsPanel />);
    await waitFor(() => expect(screen.getByText('NLP')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Reset feedback for NLP/i }));
    await waitFor(() => expect(deleteRecommendationFeedback).toHaveBeenCalledWith(5));
  });

  it('onError fires toast.error on reset failure', async () => {
    (fetchRecommendationFeedback as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [{ paper_id: 1, title: 'P', signal: 'negative', source: 'pulse_thumbs', reason: null, topic_id: 5, topic_name: 'NLP', created_at: '...' }],
      total: 1,
    });
    (deleteRecommendationFeedback as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    wrap(<RejectedTopicsPanel />);
    await waitFor(() => expect(screen.getByText('NLP')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Reset feedback for NLP/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });
});

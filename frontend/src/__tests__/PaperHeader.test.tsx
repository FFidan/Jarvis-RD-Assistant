import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PaperHeader } from '@/components/paper/PaperHeader';
import type { Paper } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    savePaper: vi.fn().mockResolvedValue({}),
    skipPaper: vi.fn().mockResolvedValue({}),
    markReading: vi.fn().mockResolvedValue({}),
    markDone: vi.fn().mockResolvedValue({}),
    trashPaper: vi.fn().mockResolvedValue({}),
    restorePaper: vi.fn().mockResolvedValue({}),
    starPaper: vi.fn().mockResolvedValue({}),
    unstarPaper: vi.fn().mockResolvedValue({}),
    hardDeletePaper: vi.fn().mockResolvedValue({ deleted: 1 }),
    submitFeedback: vi.fn().mockResolvedValue({}),
  };
});

vi.mock('sonner', () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}));

const mockPaper: Paper = {
  id: 1,
  external_id: '1706.03762',
  title: 'Test Paper: Attention Is All You Need',
  authors: ['Ashish Vaswani', 'Noam Shazeer', 'Parmar Adir'],
  abstract: 'This paper introduces the Transformer architecture.',
  source_type: 'arxiv',
  url: 'https://arxiv.org/abs/1706.03762',
  published_date: '2017-06-12',
  created_at: '2024-01-01',
  pdf_url: null,
  pdf_local_path: null,
  pdf_downloaded: false,
  priority_score: 0.8,
  citation_count: 50000,
  metadata: {},
  discovered_at: null,
};

function renderWithProviders(
  paper: Paper & {
    user_state?: { state?: string; starred?: boolean } | null;
    discovery_origin?: string;
    recent_feedback?: { signal: 'positive' | 'negative' } | null;
  } = mockPaper,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <PaperHeader paper={paper as Parameters<typeof PaperHeader>[0]['paper']} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, queryClient };
}

describe('PaperHeader', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders paper title', () => {
    renderWithProviders();
    expect(screen.getByText('Test Paper: Attention Is All You Need')).toBeInTheDocument();
  });

  it('renders paper authors', () => {
    renderWithProviders();
    expect(screen.getByText(/Ashish Vaswani, Noam Shazeer, Parmar Adir/)).toBeInTheDocument();
  });

  it('renders source badge', () => {
    renderWithProviders();
    expect(screen.getByText('arxiv')).toBeInTheDocument();
  });

  it('renders published date', () => {
    renderWithProviders();
    expect(screen.getByText(/Published:/)).toBeInTheDocument();
  });

  it('renders citation count badge', () => {
    renderWithProviders();
    expect(screen.getByText('50000 citations')).toBeInTheDocument();
  });

  it('renders external link button with valid URL', () => {
    renderWithProviders();
    const linkButton = screen.getByRole('link', { name: /Open original/ });
    expect(linkButton).toHaveAttribute('href', 'https://arxiv.org/abs/1706.03762');
    expect(linkButton).toHaveAttribute('target', '_blank');
    expect(linkButton).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('does not render external link when URL is missing', () => {
    const paperNoUrl = { ...mockPaper, url: '' };
    renderWithProviders(paperNoUrl);
    const linkButtons = screen.queryAllByRole('link');
    expect(linkButtons).toHaveLength(0);
  });

  // --- State-contextual button tests ---

  describe('state=inbox (default)', () => {
    it('renders Save as primary action when no user_state', () => {
      renderWithProviders();
      expect(screen.getByRole('button', { name: /Save paper/ })).toBeInTheDocument();
    });

    it('renders Skip button for inbox state', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'inbox' } });
      expect(screen.getByRole('button', { name: /Skip paper/ })).toBeInTheDocument();
    });

    it('renders Star toggle and Trash for inbox state', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'inbox' } });
      expect(screen.getByRole('button', { name: /Star paper/ })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Trash paper/ })).toBeInTheDocument();
    });
  });

  describe('state=to_read', () => {
    it('renders Start Reading as primary action', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'to_read' } });
      expect(screen.getByRole('button', { name: /Mark as reading/ })).toBeInTheDocument();
    });
  });

  describe('state=reading', () => {
    it('renders Mark Done and Set Aside buttons', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'reading', starred: false } });
      expect(screen.getByRole('button', { name: /Mark as done/ })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Set aside/ })).toBeInTheDocument();
    });
  });

  describe('state=done', () => {
    it('renders Re-open button', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'done' } });
      expect(screen.getByRole('button', { name: /Re-open paper/ })).toBeInTheDocument();
    });
  });

  describe('state=trash', () => {
    it('renders Restore and Delete forever buttons', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'trash' } });
      expect(screen.getByRole('button', { name: /Restore paper/ })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Delete paper forever/ })).toBeInTheDocument();
    });

    it('does not render FeedbackButtons on trash state', () => {
      renderWithProviders({
        ...mockPaper,
        user_state: { state: 'trash' },
        discovery_origin: 'pulse',
      });
      // FeedbackButtons aria-labels
      expect(screen.queryByRole('button', { name: /Recommend more like this/ })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Don't recommend like this/ })).not.toBeInTheDocument();
    });

    it('does not render Star toggle on trash state', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'trash' } });
      expect(screen.queryByRole('button', { name: /Star paper/ })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Starred/ })).not.toBeInTheDocument();
    });

    it('does not render Trash button on trash state', () => {
      renderWithProviders({ ...mockPaper, user_state: { state: 'trash' } });
      expect(screen.queryByRole('button', { name: /Trash paper/ })).not.toBeInTheDocument();
    });
  });

  // --- Mutation tests ---

  describe('mutation calls', () => {
    it('calls savePaper when Save button clicked (inbox state)', async () => {
      const { savePaper } = await import('@/lib/api');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'inbox' } });

      const saveButton = screen.getByRole('button', { name: /Save paper/ });
      await user.click(saveButton);

      await waitFor(() => {
        expect(savePaper).toHaveBeenCalledWith(1);
      });
    });

    it('calls markReading when Start Reading clicked (to_read state)', async () => {
      const { markReading } = await import('@/lib/api');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'to_read' } });

      const btn = screen.getByRole('button', { name: /Mark as reading/ });
      await user.click(btn);

      await waitFor(() => {
        expect(markReading).toHaveBeenCalledWith(1);
      });
    });

    it('calls markDone when Mark Done clicked (reading state)', async () => {
      const { markDone } = await import('@/lib/api');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'reading' } });

      const btn = screen.getByRole('button', { name: /Mark as done/ });
      await user.click(btn);

      await waitFor(() => {
        expect(markDone).toHaveBeenCalledWith(1);
      });
    });

    it('calls restorePaper when Restore clicked (trash state)', async () => {
      const { restorePaper } = await import('@/lib/api');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'trash' } });

      const btn = screen.getByRole('button', { name: /Restore paper/ });
      await user.click(btn);

      await waitFor(() => {
        expect(restorePaper).toHaveBeenCalledWith(1);
      });
    });

    it('calls starPaper when star button clicked and not starred', async () => {
      const { starPaper } = await import('@/lib/api');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'inbox', starred: false } });

      const btn = screen.getByRole('button', { name: /Star paper/ });
      await user.click(btn);

      await waitFor(() => {
        expect(starPaper).toHaveBeenCalledWith(1);
      });
    });

    it('calls unstarPaper when star button clicked and already starred', async () => {
      const { unstarPaper } = await import('@/lib/api');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'inbox', starred: true } });

      const btn = screen.getByRole('button', { name: /Starred/ });
      await user.click(btn);

      await waitFor(() => {
        expect(unstarPaper).toHaveBeenCalledWith(1);
      });
    });
  });

  // --- NI-3 error toast tests ---

  describe('NI-3 onError toasts', () => {
    it('shows error toast with description when savePaper fails', async () => {
      const { savePaper } = await import('@/lib/api');
      const error = new Error('Network error');
      vi.mocked(savePaper).mockRejectedValue(error);

      const { toast } = await import('sonner');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'inbox' } });

      const saveButton = screen.getByRole('button', { name: /Save paper/ });
      await user.click(saveButton);

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to save', {
          description: 'Network error',
        });
      });
    });

    it('shows error toast with unknown error description for non-Error rejection', async () => {
      const { savePaper } = await import('@/lib/api');
      vi.mocked(savePaper).mockRejectedValue('string error');

      const { toast } = await import('sonner');
      const user = userEvent.setup();
      renderWithProviders({ ...mockPaper, user_state: { state: 'inbox' } });

      const saveButton = screen.getByRole('button', { name: /Save paper/ });
      await user.click(saveButton);

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to save', {
          description: 'Unknown error',
        });
      });
    });
  });

  // --- Invalidation tests ---

  describe('query invalidation', () => {
    it('invalidates papers-feed and paper-detail after save', async () => {
      const { savePaper } = await import('@/lib/api');
      vi.mocked(savePaper).mockResolvedValue({} as never);

      const user = userEvent.setup();
      const { queryClient } = renderWithProviders({ ...mockPaper, user_state: { state: 'inbox' } });
      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      const saveButton = screen.getByRole('button', { name: /Save paper/ });
      await user.click(saveButton);

      await waitFor(() => {
        const keys = invalidateSpy.mock.calls.map(
          ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
        );
        expect(keys).toContainEqual(['papers-feed']);
        expect(keys).toContainEqual(['paper-detail', 1]);
      });
    });
  });
});

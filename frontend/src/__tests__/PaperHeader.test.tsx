import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PaperHeader } from '@/components/paper/PaperHeader';
import type { Paper } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    bookmarkPaper: vi.fn(),
  };
});

vi.mock('sonner', () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
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
  paper: Paper = mockPaper,
  isStarred: boolean = false,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PaperHeader paper={paper} isStarred={isStarred} />
    </QueryClientProvider>,
  );
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

  it('renders bookmark button', () => {
    renderWithProviders();
    const bookmarkButton = screen.getByRole('button', { name: /Bookmark paper/ });
    expect(bookmarkButton).toBeInTheDocument();
  });

  it('renders filled star when isStarred=true', () => {
    renderWithProviders(mockPaper, true);
    const bookmarkButton = screen.getByRole('button', { name: /Bookmarked/ });
    expect(bookmarkButton).toBeInTheDocument();
    const star = bookmarkButton.querySelector('svg');
    expect(star?.className.baseVal).toContain('fill-yellow-400');
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

  it('calls bookmarkPaper mutation on bookmark button click', async () => {
    const { bookmarkPaper } = await import('@/lib/api');
    vi.mocked(bookmarkPaper).mockResolvedValue({ status: 'bookmarked', paper_id: 1 });

    const user = userEvent.setup();
    renderWithProviders();

    const bookmarkButton = screen.getByRole('button', { name: /Bookmark paper/ });
    await user.click(bookmarkButton);

    await waitFor(() => {
      expect(bookmarkPaper).toHaveBeenCalledWith(1);
    });
  });

  it('shows error toast when bookmark mutation fails', async () => {
    const { bookmarkPaper } = await import('@/lib/api');
    const error = new Error('Network error');
    vi.mocked(bookmarkPaper).mockRejectedValue(error);

    const { toast } = await import('sonner');

    const user = userEvent.setup();
    renderWithProviders();

    const bookmarkButton = screen.getByRole('button', { name: /Bookmark paper/ });
    await user.click(bookmarkButton);

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Failed to bookmark paper');
    });
  });

  it('disables bookmark button while mutation is pending', async () => {
    const { bookmarkPaper } = await import('@/lib/api');
    // Simulate a slow promise
    let resolveBookmark: () => void;
    const bookmarkPromise = new Promise<{ status: string; paper_id: number }>((resolve) => {
      resolveBookmark = () => resolve({ status: 'bookmarked', paper_id: 1 });
    });
    vi.mocked(bookmarkPaper).mockReturnValue(bookmarkPromise);

    const user = userEvent.setup();
    renderWithProviders();

    const bookmarkButton = screen.getByRole('button', { name: /Bookmark paper/ });
    await user.click(bookmarkButton);

    // Button should be disabled while pending
    await waitFor(() => {
      expect(bookmarkButton).toBeDisabled();
    });

    // Resolve the mutation
    resolveBookmark!();

    // Button should be enabled again
    await waitFor(() => {
      expect(bookmarkButton).not.toBeDisabled();
    });
  });
});

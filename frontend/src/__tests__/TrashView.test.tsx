import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TrashView } from '@/components/feed/TrashView';
import { HardDeleteModal } from '@/components/feed/HardDeleteModal';

// Mock FeedView so we can verify props without rendering the full feed
vi.mock('@/components/feed/FeedView', () => ({
  FeedView: ({ surface }: { surface: string; filter?: string | null }) => (
    <div data-testid="feed-view" data-surface={surface} />
  ),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    hardDeletePaper: vi.fn().mockResolvedValue({ ok: true }),
  };
});

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderTrashView() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter>
        <TrashView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('TrashView', () => {
  it('renders the trash warning banner', () => {
    renderTrashView();
    expect(
      screen.getByText(/Papers in Trash will be kept until you delete them forever/i),
    ).toBeInTheDocument();
  });

  it('renders FeedView with surface="trash"', () => {
    renderTrashView();
    const feedView = screen.getByTestId('feed-view');
    expect(feedView).toBeInTheDocument();
    expect(feedView).toHaveAttribute('data-surface', 'trash');
  });
});

describe('HardDeleteModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderModal(paperTitle = 'My Test Paper', paperId = 42) {
    return render(
      <QueryClientProvider client={makeQueryClient()}>
        <HardDeleteModal
          open
          paperId={paperId}
          paperTitle={paperTitle}
          onClose={vi.fn()}
        />
      </QueryClientProvider>,
    );
  }

  it('Delete-forever button is disabled when confirm text is wrong', async () => {
    renderModal('Exact Title');
    const input = screen.getByPlaceholderText('Paper title');
    await userEvent.type(input, 'wrong title');
    expect(screen.getByRole('button', { name: 'Delete forever' })).toBeDisabled();
  });

  it('Delete-forever button is enabled when confirm text matches exactly', async () => {
    renderModal('Exact Title');
    const input = screen.getByPlaceholderText('Paper title');
    await userEvent.type(input, 'Exact Title');
    expect(screen.getByRole('button', { name: 'Delete forever' })).toBeEnabled();
  });

  it('calls hardDeletePaper with correct args on confirm', async () => {
    const { hardDeletePaper } = await import('@/lib/api');
    renderModal('Exact Title', 99);
    const input = screen.getByPlaceholderText('Paper title');
    await userEvent.type(input, 'Exact Title');
    await userEvent.click(screen.getByRole('button', { name: 'Delete forever' }));
    expect(hardDeletePaper).toHaveBeenCalledWith(99, {
      confirm_title: 'Exact Title',
      also_zotero: false,
    });
  });

  it('shows error toast when hard delete fails', async () => {
    const { hardDeletePaper } = await import('@/lib/api');
    vi.mocked(hardDeletePaper).mockRejectedValueOnce(new Error('Title mismatch'));
    const { toast } = await import('sonner');
    renderModal('Exact Title', 55);
    const input = screen.getByPlaceholderText('Paper title');
    await userEvent.type(input, 'Exact Title');
    await userEvent.click(screen.getByRole('button', { name: 'Delete forever' }));
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Title mismatch');
    });
  });

  it('enables button and sends trimmed title when user types with surrounding whitespace', async () => {
    const { hardDeletePaper } = await import('@/lib/api');
    vi.mocked(hardDeletePaper).mockResolvedValue({ deleted: 1 });
    renderModal('My Paper', 7);
    const input = screen.getByPlaceholderText('Paper title');
    // Type title with leading and trailing spaces
    await userEvent.type(input, '  My Paper  ');
    // Button should be enabled because trimmed input matches trimmed paperTitle
    expect(screen.getByRole('button', { name: 'Delete forever' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Delete forever' }));
    // API must receive the trimmed value, not the raw padded string
    expect(hardDeletePaper).toHaveBeenCalledWith(7, {
      confirm_title: 'My Paper',
      also_zotero: false,
    });
  });
});

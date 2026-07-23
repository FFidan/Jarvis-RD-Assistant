import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { HardDeleteModal } from '@/components/feed/HardDeleteModal';

vi.mock('@/lib/api', () => ({ hardDeletePaper: vi.fn() }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import { hardDeletePaper } from '@/lib/api';
import { toast } from 'sonner';

const wrap = (ui: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
};

describe('HardDeleteModal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders confirm dialog when open', () => {
    wrap(<HardDeleteModal open={true} onOpenChange={vi.fn()} paperId={1} paperTitle="My Paper" />);
    expect(screen.getByText(/Permanently delete this paper\?/i)).toBeInTheDocument();
    expect(screen.getByText(/removed from your library/i)).toBeInTheDocument();
    expect(screen.getByText(/shared search content may remain/i)).toBeInTheDocument();
    expect(screen.getByText(/does not remove the paper system-wide/i)).toBeInTheDocument();
    expect(screen.queryByText(/removed from JARVIS/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Delete$/i })).toBeInTheDocument();
  });

  it('describes bulk removal as caller-scoped', () => {
    wrap(
      <HardDeleteModal
        count={2}
        onConfirm={vi.fn()}
        trigger={<button type="button">Open bulk delete</button>}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Open bulk delete' }));
    expect(screen.getByText(/removed from your library/i)).toBeInTheDocument();
    expect(screen.getByText(/shared search content may remain/i)).toBeInTheDocument();
    expect(screen.getByText(/does not remove these papers system-wide/i)).toBeInTheDocument();
    expect(screen.queryByText(/removed from the database and search index/i)).not.toBeInTheDocument();
  });

  it('clicking Delete calls hardDeletePaper', async () => {
    (hardDeletePaper as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);
    wrap(<HardDeleteModal open={true} onOpenChange={vi.fn()} paperId={42} paperTitle="X" />);
    fireEvent.click(screen.getByRole('button', { name: /^Delete$/i }));
    await waitFor(() => expect(hardDeletePaper).toHaveBeenCalledWith(42));
  });

  it('onError fires toast.error on mutation failure', async () => {
    (hardDeletePaper as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    wrap(<HardDeleteModal open={true} onOpenChange={vi.fn()} paperId={1} paperTitle="X" />);
    fireEvent.click(screen.getByRole('button', { name: /^Delete$/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });

  it('does not require typing the title (no input field)', () => {
    wrap(<HardDeleteModal open={true} onOpenChange={vi.fn()} paperId={1} paperTitle="X" />);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});

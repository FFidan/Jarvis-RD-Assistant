import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PairTelegram } from '@/components/setup/PairTelegram';

vi.mock('@/lib/api', () => ({
  createPairingCode: vi.fn(),
  getPairingStatus: vi.fn(),
  unpairTelegram: vi.fn().mockResolvedValue(undefined),
}));

const api = await import('@/lib/api');

function renderPair(onPaired?: () => void) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PairTelegram onPaired={onPaired} />
    </QueryClientProvider>,
  );
}

describe('PairTelegram', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: not currently paired.
    vi.mocked(api.getPairingStatus).mockResolvedValue({ paired: false, chat_id: null });
  });

  it('renders idle state with generate button', async () => {
    renderPair();
    expect(
      await screen.findByRole('button', { name: /generate pairing code/i }),
    ).toBeInTheDocument();
  });

  it('transitions to polling state after clicking generate', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createPairingCode).mockResolvedValue({
      code: 'ABC123XYZ789',
      deep_link: 'https://t.me/testbot?start=ABC123XYZ789',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    });
    renderPair();
    await user.click(
      await screen.findByRole('button', { name: /generate pairing code/i }),
    );
    expect(await screen.findByText('ABC123XYZ789')).toBeInTheDocument();
    expect(screen.getByText(/waiting for confirmation/i)).toBeInTheDocument();
  });

  it('transitions to paired state when the server reports paired=true', async () => {
    const user = userEvent.setup();
    const onPaired = vi.fn();
    vi.mocked(api.createPairingCode).mockResolvedValue({
      code: 'ABC123XYZ789',
      deep_link: 'https://t.me/testbot?start=ABC123XYZ789',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    });
    // First status call (initial) returns unpaired; subsequent returns paired.
    vi.mocked(api.getPairingStatus)
      .mockResolvedValueOnce({ paired: false, chat_id: null })
      .mockResolvedValue({ paired: true, chat_id: 12345 });

    renderPair(onPaired);
    await user.click(
      await screen.findByRole('button', { name: /generate pairing code/i }),
    );
    await waitFor(
      () => {
        expect(screen.getByText(/paired with chat id/i)).toBeInTheDocument();
      },
      { timeout: 5000 },
    );
    expect(screen.getByText('12345')).toBeInTheDocument();
    expect(onPaired).toHaveBeenCalled();
  });

  it('unpair click returns to idle', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getPairingStatus).mockResolvedValue({ paired: true, chat_id: 98765 });
    renderPair();
    expect(await screen.findByText(/paired with chat id/i)).toBeInTheDocument();
    // Simulate the server now reporting unpaired so cache refresh doesn't bounce back.
    vi.mocked(api.getPairingStatus).mockResolvedValue({ paired: false, chat_id: null });
    await user.click(screen.getByRole('button', { name: /unpair/i }));
    await waitFor(() => {
      expect(api.unpairTelegram).toHaveBeenCalled();
    });
    expect(
      await screen.findByRole('button', { name: /generate pairing code/i }),
    ).toBeInTheDocument();
  });

  it('shows bot_missing warning when bot_username_missing is true', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createPairingCode).mockResolvedValue({
      code: 'NOBOT123456',
      deep_link: '',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
      bot_username_missing: true,
    });
    renderPair();
    await user.click(
      await screen.findByRole('button', { name: /generate pairing code/i }),
    );
    expect(await screen.findByText(/bot username unknown/i)).toBeInTheDocument();
    expect(screen.getByText('NOBOT123456')).toBeInTheDocument();
  });

  it('renders deep_link as anchor with target="_blank" when URL is a valid t.me link', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createPairingCode).mockResolvedValue({
      code: 'VALID123456',
      deep_link: 'https://t.me/testbot?start=VALID123456',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    });
    renderPair();
    await user.click(
      await screen.findByRole('button', { name: /generate pairing code/i }),
    );
    const link = await screen.findByRole('link', { name: /open in telegram/i });
    expect(link).toHaveAttribute('href', 'https://t.me/testbot?start=VALID123456');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('shows "Invalid pairing link" when deep_link is not a t.me URL', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createPairingCode).mockResolvedValue({
      code: 'BADLINK123456',
      deep_link: 'https://evil.example.com/start=BADLINK123456',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    });
    renderPair();
    await user.click(
      await screen.findByRole('button', { name: /generate pairing code/i }),
    );
    expect(await screen.findByText(/invalid pairing link/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /open in telegram/i })).not.toBeInTheDocument();
  });
});

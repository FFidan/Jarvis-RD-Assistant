/**
 * FetchCitationsButton vitest (M7.3a / GC-02).
 *
 * The fetch loop used to throw on the first failure, discarding partial
 * progress. It now uses Promise.allSettled: every paper is attempted, the
 * outcome reports "X of Y succeeded (Z failed)", and a partial failure exposes
 * a retry scoped to only the failed ids. A partial failure must surface a
 * degraded/partial state — never a blanket error or an empty state.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/lib/api', () => ({
  fetchCitationsFromS2: vi.fn(),
}));

import { fetchCitationsFromS2 } from '@/lib/api';
import { FetchCitationsButton } from '@/components/citation/FetchCitationsButton';

const mockFetch = vi.mocked(fetchCitationsFromS2);

const mkQc = () =>
  new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });

const wrap = (ui: React.ReactNode) =>
  render(<QueryClientProvider client={mkQc()}>{ui}</QueryClientProvider>);

const ok = (citations: number, references: number) =>
  Promise.resolve({ citations_added: citations, references_added: references, stubs_created: 0 });

beforeEach(() => vi.clearAllMocks());

describe('FetchCitationsButton', () => {
  it('reports a clean all-success run with aggregated counts', async () => {
    mockFetch.mockReturnValueOnce(ok(3, 5)).mockReturnValueOnce(ok(1, 2));
    const user = userEvent.setup();
    wrap(<FetchCitationsButton paperIds={[10, 20]} />);

    await user.click(screen.getByRole('button', { name: /fetch citations/i }));

    await waitFor(() =>
      expect(screen.getByText(/2 of 2 succeeded \(0 failed\)/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/4 citations, 7 references/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('keeps partial progress on mixed success/failure and offers a failed-id retry', async () => {
    mockFetch
      .mockReturnValueOnce(ok(2, 4)) // id 10 ok
      .mockRejectedValueOnce(new Error('S2 down')) // id 20 fails
      .mockReturnValueOnce(ok(1, 1)); // id 30 ok
    const user = userEvent.setup();
    wrap(<FetchCitationsButton paperIds={[10, 20, 30]} />);

    await user.click(screen.getByRole('button', { name: /^fetch citations/i }));

    // Degraded/partial state — not a blanket error, not empty.
    await waitFor(() =>
      expect(screen.getByText(/2 of 3 succeeded \(1 failed\)/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/3 citations, 5 references/i)).toBeInTheDocument();

    // Retry runs ONLY the failed id (20), not the whole set.
    mockFetch.mockReturnValueOnce(ok(7, 8));
    await user.click(screen.getByRole('button', { name: /retry 1 failed/i }));

    await waitFor(() => expect(mockFetch).toHaveBeenLastCalledWith(20));
    await waitFor(() =>
      expect(screen.getByText(/1 of 1 succeeded \(0 failed\)/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('shows a degraded state when every fetch fails, with retry of all ids', async () => {
    mockFetch
      .mockRejectedValueOnce(new Error('boom'))
      .mockRejectedValueOnce(new Error('boom'));
    const user = userEvent.setup();
    wrap(<FetchCitationsButton paperIds={[10, 20]} />);

    await user.click(screen.getByRole('button', { name: /^fetch citations/i }));

    await waitFor(() =>
      expect(screen.getByText(/0 of 2 succeeded \(2 failed\)/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: /retry 2 failed/i })).toBeInTheDocument();
  });

  it('disables the trigger when paperIds is empty', () => {
    wrap(<FetchCitationsButton paperIds={[]} />);
    expect(screen.getByRole('button', { name: /fetch citations/i })).toBeDisabled();
  });
});

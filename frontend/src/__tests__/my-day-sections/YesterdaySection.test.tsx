import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { YesterdaySection } from '@/components/my-day/sections/YesterdaySection';
import type { YesterdaySummary } from '@/types';

vi.mock('@/lib/api', () => ({
  fetchYesterday: vi.fn(),
  updateTask: vi.fn(),
}));

const { fetchYesterday, updateTask } = await import('@/lib/api');

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <YesterdaySection />
    </QueryClientProvider>,
  );
}

const SUMMARY: YesterdaySummary = {
  date: '2026-05-14',
  focused_hours: 3.2,
  cards_reviewed: 6,
  tasks_done: 2,
  completed: [{ id: 1, title: 'Solver benchmark compiled', status: 'done' }],
  deferred: [{ id: 2, title: 'Adjoint memory bound', status: 'deferred' }],
};

describe('YesterdaySection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(updateTask).mockResolvedValue(undefined as never);
  });

  it('stays silent when there was no recorded activity', async () => {
    vi.mocked(fetchYesterday).mockResolvedValue({
      ...SUMMARY,
      completed: [],
      deferred: [],
    });
    const { container } = renderSection();
    // give the query a tick
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector('#yesterday')).toBeNull();
  });

  it('renders the header note + completed and deferred items', async () => {
    vi.mocked(fetchYesterday).mockResolvedValue(SUMMARY);
    renderSection();
    expect(await screen.findByText(/§ Yesterday/i)).toBeInTheDocument();
    expect(screen.getByText(/3.2h focused · 6 cards · 2 tasks done/)).toBeInTheDocument();
    expect(screen.getByText('Solver benchmark compiled')).toBeInTheDocument();
    expect(screen.getByText('Adjoint memory bound')).toBeInTheDocument();
  });

  it('"carry over →" reopens the deferred task into today', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchYesterday).mockResolvedValue(SUMMARY);
    renderSection();

    const carry = await screen.findByRole('button', { name: /carry over/i });
    await user.click(carry);

    expect(vi.mocked(updateTask)).toHaveBeenCalledWith(2, { status: 'todo' });
  });

  it('renders error sentinel when query fails', async () => {
    vi.mocked(fetchYesterday).mockRejectedValue(new Error('network'));
    renderSection();
    expect(await screen.findByRole('status')).toHaveTextContent(/unable to load/i);
  });
});

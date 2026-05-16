import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ThreadsSection } from '@/components/my-day/sections/ThreadsSection';
import type { Thread } from '@/types';

vi.mock('@/lib/api', () => ({
  fetchThreads: vi.fn(),
  createThread: vi.fn(),
  resumeThread: vi.fn(),
}));

const { fetchThreads, createThread, resumeThread } = await import('@/lib/api');

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ThreadsSection />
    </QueryClientProvider>,
  );
}

const THREAD: Thread = {
  id: 3,
  title: 'Memory-bound derivation',
  anchor: 'notebook §4.2',
  progress: 0.85,
  last_at: '2026-05-15T09:02:00Z',
  status: 'open',
  created_at: '2026-05-10T00:00:00Z',
};

describe('ThreadsSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(createThread).mockResolvedValue(THREAD);
    vi.mocked(resumeThread).mockResolvedValue(THREAD);
  });

  it('shows the empty affordance when there are no open threads', async () => {
    vi.mocked(fetchThreads).mockResolvedValue([]);
    renderSection();
    expect(await screen.findByText(/No open threads/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /\+ new thread/i })).toBeInTheDocument();
  });

  it('renders thread rows with progress and a resume action', async () => {
    vi.mocked(fetchThreads).mockResolvedValue([THREAD]);
    renderSection();
    expect(await screen.findByText('Memory-bound derivation')).toBeInTheDocument();
    expect(screen.getByText(/↳ notebook §4.2/)).toBeInTheDocument();
    expect(screen.getByText(/85% ·/)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /resume →/i }));
    expect(vi.mocked(resumeThread)).toHaveBeenCalledWith(3);
  });

  it('creates a user thread via the inline form', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchThreads).mockResolvedValue([]);
    renderSection();

    await user.click(await screen.findByRole('button', { name: /\+ new thread/i }));
    await user.type(screen.getByLabelText('Thread title'), 'New line of work');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    expect(vi.mocked(createThread)).toHaveBeenCalledWith({
      title: 'New line of work',
      anchor: null,
    });
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConsensusPage } from '@/pages/ConsensusPage';

const fetchConsensusMock = vi.fn();
const scanContradictionsMock = vi.fn().mockResolvedValue({ job_id: 'x', status: 'queued' });
vi.mock('@/lib/api', () => ({
  fetchConsensus: () => fetchConsensusMock(),
  scanContradictions: () => scanContradictionsMock(),
}));

vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (s: { trackExternalJob: () => void; isRunning: () => boolean }) => unknown) =>
    selector({ trackExternalJob: () => {}, isRunning: () => false }),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/consensus']}>
        <ConsensusPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchConsensusMock.mockReset();
  scanContradictionsMock.mockClear();
});

describe('ConsensusPage', () => {
  it('renders claim clusters with stance counts and click-through to verified quotes', async () => {
    fetchConsensusMock.mockResolvedValue({
      total: 1,
      claims: [
        {
          claim_topic: 'effect of X on Y',
          supports: 2,
          opposes: 1,
          paper_ids: [1, 2, 3],
          assessments: [
            {
              stance: 'supports',
              paper_a_title: 'Paper A',
              paper_b_title: 'Paper B',
              quote_a: 'A supports X',
              quote_b: 'B supports X',
              page_a: 3,
              page_b: 5,
            },
          ],
        },
      ],
    });

    renderPage();

    expect(await screen.findByText(/2 support/)).toBeInTheDocument();
    expect(screen.getByText(/1 oppose/)).toBeInTheDocument();

    // Evidence is hidden until the user expands it (click-through to the quote).
    expect(screen.queryByText(/A supports X/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /show evidence/i }));
    expect(await screen.findByText(/A supports X/)).toBeInTheDocument();
  });

  it('shows an honest empty state when there are no claims', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    renderPage();
    expect(await screen.findByText('No related-paper claims yet')).toBeInTheDocument();
  });

  it('runs a consensus scan from the empty-state CTA', async () => {
    fetchConsensusMock.mockResolvedValue({ total: 0, claims: [] });
    renderPage();
    const cta = await screen.findByRole('button', { name: /run consensus scan/i });
    await userEvent.click(cta);
    expect(scanContradictionsMock).toHaveBeenCalledTimes(1);
  });

  it('shows a degraded state when the fetch fails', async () => {
    fetchConsensusMock.mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByText(/Failed to load consensus/)).toBeInTheDocument();
  });
});

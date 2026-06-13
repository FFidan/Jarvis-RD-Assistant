import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WeeklyDigestSection } from '@/components/my-day/sections/WeeklyDigestSection';
import * as api from '@/lib/api';
import type { WeeklyDigestResponse } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchWeeklyDigest: vi.fn(),
  };
});

const digestWithMixedVerification: WeeklyDigestResponse = {
  topics: [
    {
      name: 'Efficient Transformers',
      paper_count: 3,
      summary: 'Three papers on efficient attention this week.',
      themes: [
        {
          theme: 'Novel attention mechanisms reduce computational complexity.',
          supporting_papers: [1, 2],
          notes: null,
          verified: true,
          verification_reason: null,
        },
        {
          theme: 'Sparse routing reduces inference cost by an order of magnitude.',
          supporting_papers: [3],
          notes: null,
          verified: false,
          verification_reason:
            'theme text not supported by source papers (best fuzzy match: 50%)',
        },
        {
          theme: 'Kernel choice impacts efficiency and performance.',
          supporting_papers: [2],
          notes: null,
          verified: null,
          verification_reason: null,
        },
      ],
      top_papers: [],
    },
  ],
  total_papers: 3,
  period_start: '2026-06-05T00:00:00Z',
  period_end: '2026-06-12T00:00:00Z',
};

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <WeeklyDigestSection />
    </QueryClientProvider>,
  );
}

describe('WeeklyDigestSection theme verification badges', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchWeeklyDigest).mockResolvedValue(digestWithMixedVerification);
  });

  it('renders one row per theme with the correct badge per verification state', async () => {
    renderSection();

    const rows = await screen.findAllByTestId('digest-theme-row');
    expect(rows).toHaveLength(3);

    // verified: true → green Verified badge on exactly one row
    const verifiedBadges = screen.getAllByTestId('digest-theme-verified');
    expect(verifiedBadges).toHaveLength(1);
    expect(verifiedBadges[0]).toHaveTextContent('Verified');

    // verified: false → amber Unverified badge on exactly one row
    const unverifiedBadges = screen.getAllByTestId('digest-theme-unverified');
    expect(unverifiedBadges).toHaveLength(1);
    expect(unverifiedBadges[0]).toHaveTextContent('Unverified');

    // verified: null → no badge at all (verification not attempted)
    expect(
      screen.getByText('Kernel choice impacts efficiency and performance.'),
    ).toBeInTheDocument();
  });

  it('keeps theme text visible alongside the badges', async () => {
    renderSection();

    expect(
      await screen.findByText('Novel attention mechanisms reduce computational complexity.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Sparse routing reduces inference cost by an order of magnitude.'),
    ).toBeInTheDocument();
  });
});

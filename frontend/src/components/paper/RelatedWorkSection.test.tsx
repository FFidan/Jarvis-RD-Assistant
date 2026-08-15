import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createTestQueryClient } from '@/__tests__/test-utils';
import { getCitationGraph } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { RelatedWorkSection } from './RelatedWorkSection';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getCitationGraph: vi.fn(),
    fetchCitationsFromS2: vi.fn(),
  };
});

function renderSection(metadata: Record<string, unknown>, graph: unknown) {
  const client = createTestQueryClient();
  client.setQueryData(QUERY_KEYS.papers.detail(7), {
    paper: {
      external_id: 'local:unindexed.pdf',
      metadata,
    },
  });
  vi.mocked(getCitationGraph).mockResolvedValue(graph as Awaited<ReturnType<typeof getCitationGraph>>);
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <RelatedWorkSection paperId={7} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe('RelatedWorkSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders an unavailable cited-by state for an unidentified upload', async () => {
    renderSection({}, {
      nodes: [{
        id: 7,
        title: 'Unindexed manuscript',
        citation_count: 0,
        published_date: null,
        is_stub: false,
      }],
      edges: [],
    });

    expect(await screen.findByTestId('citation-cited-by-unavailable')).toHaveTextContent(
      'Citation-index data is unavailable because this document has not been identified',
    );
    expect(screen.queryByTestId('citation-cited-by')).not.toBeInTheDocument();
    expect(screen.queryByText('No citation data yet for this paper.')).not.toBeInTheDocument();
    expect(getCitationGraph).toHaveBeenCalledTimes(1);
  });

  it('renders resolved graph rows and retained unresolved bibliography text', async () => {
    renderSection({
      bibliography: [
        {
          raw_text: '[1] Rivera, M. Notes on laboratory indexing. 2021.',
          title: 'Notes on laboratory indexing',
          authors: ['Rivera, M.'],
          year: 2021,
          venue: 'Institute Technical Report',
          resolved: false,
        },
      ],
    }, {
      nodes: [
        {
          id: 7,
          title: 'Unindexed manuscript',
          citation_count: 0,
          published_date: null,
          is_stub: false,
        },
        {
          id: 11,
          title: 'Attention Is All You Need',
          citation_count: 100,
          published_date: '2017-01-01',
          is_stub: true,
        },
      ],
      edges: [{ source: 7, target: 11, is_influential: false, context: null }],
    });

    expect(await screen.findByRole('link', { name: 'Attention Is All You Need' })).toHaveAttribute(
      'href',
      '/paper/11',
    );
    expect(
      screen.getByText('[1] Rivera, M. Notes on laboratory indexing. 2021.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'Notes on laboratory indexing · Rivera, M. · 2021 · Institute Technical Report',
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId('citation-cited-by-unavailable')).toBeInTheDocument();
    expect(getCitationGraph).toHaveBeenCalledTimes(1);
  });
});

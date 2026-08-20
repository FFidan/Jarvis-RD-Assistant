import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PreviewResults } from '@/components/feed/PreviewResults';
import type { SearchPreviewResult } from '@/types';

vi.mock('@/components/feed/SearchPreviewRow', () => ({
  SearchPreviewRow: ({ paper }: { paper: SearchPreviewResult }) => (
    <div data-testid={`row-${paper.external_id}`}>{paper.title}</div>
  ),
}));

vi.mock('@/components/feed/SearchPreviewDrawer', () => ({
  SearchPreviewDrawer: () => null,
}));

function makePaper(id: string, inLibrary: boolean): SearchPreviewResult {
  return {
    external_id: id,
    title: `Paper ${id}`,
    authors: [],
    abstract: null,
    published_date: null,
    url: '',
    pdf_url: null,
    source_type: 'arxiv',
    citation_count: 0,
    library_match: inLibrary ? { paper_id: Number(id.replace(/\D/g, '') || '1'), has_project_links: false, zotero_item_key: null } : null,
    metadata: {},
  };
}

describe('PreviewResults — library-match message (FEE-2)', () => {
  it('does NOT show excluded message when no papers are library-matched', () => {
    const papers = [makePaper('new-1', false), makePaper('new-2', false)];
    render(
      <MemoryRouter>
        <PreviewResults papers={papers} onSave={vi.fn()} onClear={vi.fn()} isSaving={false} />
      </MemoryRouter>,
    );
    expect(
      screen.queryByText(/library-matched results are already in your library/i),
    ).not.toBeInTheDocument();
  });

  it('shows excluded message when at least one paper is library-matched', () => {
    const papers = [makePaper('lib-1', true), makePaper('new-1', false)];
    render(
      <MemoryRouter>
        <PreviewResults papers={papers} onSave={vi.fn()} onClear={vi.fn()} isSaving={false} />
      </MemoryRouter>,
    );
    expect(
      screen.getByText(/library-matched results are already in your library/i),
    ).toBeInTheDocument();
  });

  it('shows all-matched message when every paper is library-matched', () => {
    const papers = [makePaper('lib-1', true), makePaper('lib-2', true)];
    render(
      <MemoryRouter>
        <PreviewResults papers={papers} onSave={vi.fn()} onClear={vi.fn()} isSaving={false} />
      </MemoryRouter>,
    );
    expect(
      screen.getByText(/all results in this preview are already in your library/i),
    ).toBeInTheDocument();
  });

  it('shows counts and degraded details in source rows', () => {
    render(
      <MemoryRouter>
        <PreviewResults
          papers={[makePaper('new-1', false)]}
          onSave={vi.fn()}
          onClear={vi.fn()}
          isSaving={false}
          perSourceCounts={{ arxiv: 1 }}
          sourceErrors={{
            semantic_scholar: {
              kind: 'rate_limit',
              message: 'Semantic Scholar rate limit reached. Retry later.',
              status_code: 429,
              retry_after_s: 3,
              settings_hint: null,
            },
            openalex: {
              kind: 'api_error',
              message: 'OpenAlex search was skipped because no API key is configured.',
              status_code: null,
              retry_after_s: null,
              settings_hint: 'Add an OpenAlex API key in Settings > Sources.',
            },
          }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId('source-summary-arxiv')).toHaveTextContent('1 result');
    expect(screen.getByTestId('source-summary-semantic_scholar')).toHaveTextContent(
      'Semantic Scholar rate limit reached. Retry later.',
    );
    expect(screen.getByTestId('source-summary-openalex')).toHaveTextContent(
      'Add an OpenAlex API key in Settings > Sources.',
    );
    // A source that failed was never searched, so it must not report "0 results"
    // as though it had looked and found nothing.
    expect(screen.getByTestId('source-summary-openalex')).toHaveTextContent('not searched');
    expect(screen.getByTestId('source-summary-openalex')).not.toHaveTextContent('0 results');
    expect(screen.getByTestId('source-summary-semantic_scholar')).not.toHaveTextContent(
      '0 results',
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

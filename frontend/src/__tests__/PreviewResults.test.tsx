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
});

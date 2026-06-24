import { describe, expect, it } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SourcesAccordion } from '@/components/chat/SourcesAccordion';
import type { Source } from '@/types';

function source(overrides: Partial<Source> = {}): Source {
  return {
    chunk_id: 1,
    paper_id: 42,
    paper_title: 'A Cited Paper',
    text: 'Some supporting passage.',
    page_number: 3,
    score: 0.9,
    ...overrides,
  };
}

function renderAccordion(sources: Source[]) {
  return render(
    <MemoryRouter>
      <SourcesAccordion sources={sources} />
    </MemoryRouter>,
  );
}

describe('SourcesAccordion', () => {
  it('renders the source title as a link to the paper when paper_id is present', () => {
    renderAccordion([source()]);
    fireEvent.click(screen.getByRole('button'));

    const link = screen.getByRole('link', { name: 'A Cited Paper' });
    expect(link).toHaveAttribute('href', '/paper/42');
  });

  it('renders the source title as plain text (no link) when paper_id is null', () => {
    renderAccordion([source({ paper_id: undefined })]);
    fireEvent.click(screen.getByRole('button'));

    expect(screen.getByText('A Cited Paper')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

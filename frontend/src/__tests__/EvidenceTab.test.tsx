import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { EvidenceTab } from '@/components/paper/EvidenceTab';
import type { KeyFinding, Summary } from '@/types';

function keyFinding(overrides: Partial<KeyFinding> = {}): KeyFinding {
  return {
    finding: 'A verified finding.',
    quote: 'A supporting quote.',
    page_number: 7,
    chunk_id: 12,
    verified: true,
    snapshot_path: null,
    ...overrides,
  };
}

function summary(findings: KeyFinding[]): Summary {
  return {
    id: 1,
    paper_id: 42,
    summary_brief: 'brief',
    summary_detailed: 'detailed',
    tldr: null,
    key_findings: findings,
    methodology: null,
    limitations: null,
    relevance_notes: null,
    confidence: 'HIGH',
    cross_references: [],
    llm_model: null,
    summary_verified: true,
    created_at: '2026-06-23T00:00:00Z',
  };
}

function renderTab(s: Summary, paperId?: number) {
  return render(
    <MemoryRouter>
      <EvidenceTab summary={s} paperId={paperId} />
    </MemoryRouter>,
  );
}

describe('EvidenceTab', () => {
  it('renders a link to the paper when paperId is present', () => {
    renderTab(summary([keyFinding()]), 42);

    const link = screen.getByRole('link', { name: /open paper/i });
    expect(link).toHaveAttribute('href', '/paper/42');
  });

  it('renders no paper link when paperId is undefined', () => {
    renderTab(summary([keyFinding()]), undefined);

    expect(screen.queryByRole('link', { name: /open paper/i })).not.toBeInTheDocument();
    expect(screen.getByText('Verified Findings')).toBeInTheDocument();
  });
});

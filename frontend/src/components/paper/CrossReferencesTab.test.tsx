import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { CrossReferencesTab } from './CrossReferencesTab';
import type { Summary } from '@/types';

const summary: Summary = {
  id: 1,
  paper_id: 7,
  summary_brief: 'Brief',
  summary_detailed: 'Detailed',
  tldr: null,
  key_findings: [],
  methodology: null,
  limitations: null,
  relevance_notes: null,
  confidence: 'HIGH',
  cross_references: [
    {
      related_paper_id: 2,
      related_title: 'Available Related Paper',
      related_year: 2023,
      relationship: 'semantic_similarity',
      explanation: 'Related evidence',
      related_quote: null,
    },
    {
      related_paper_id: 99,
      related_title: null,
      related_year: null,
      relationship: 'semantic_similarity',
      explanation: 'The target was removed',
      related_quote: null,
    },
  ],
  llm_model: null,
  summary_verified: true,
  created_at: '2026-08-15T00:00:00Z',
};

describe('CrossReferencesTab', () => {
  it('renders joined titles and a usable missing-paper row', () => {
    render(
      <MemoryRouter>
        <CrossReferencesTab summary={summary} />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: 'Available Related Paper (2023)' })).toHaveAttribute(
      'href',
      '/paper/2',
    );
    expect(screen.getByText('Related paper unavailable (ID 99)')).toBeInTheDocument();
    expect(screen.getByText('The target was removed')).toBeInTheDocument();
  });
});

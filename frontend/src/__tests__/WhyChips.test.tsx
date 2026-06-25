import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { WhyChips } from '@/components/my-day/primitives/WhyChips';
import { SIGNAL_LABELS, signalLabel } from '@/lib/labels/signals';

// Guards that the shared signals.ts map is the single source of truth.
describe('WhyChips', () => {
  it('renders human-readable labels from the shared signals map', () => {
    render(<WhyChips signals={{ embedding: 0.8, llm_relevance: 0.5 }} />);
    expect(screen.getByText(/Semantic similarity/)).toBeInTheDocument();
    expect(screen.getByText(/Relevance score/)).toBeInTheDocument();
    // the labels come from the shared signals map
    expect(SIGNAL_LABELS['embedding']).toBe('Semantic similarity');
    expect(SIGNAL_LABELS['llm_relevance']).toBe('Relevance score');
  });

  it('renders WhyChips-only short alias labels from the shared map', () => {
    render(<WhyChips signals={{ topic_match: 0.9 }} />);
    expect(screen.getByText(/Topic match/)).toBeInTheDocument();
    expect(SIGNAL_LABELS['topic_match']).toBe('Topic match');
  });

  it('signalLabel falls back to the raw key when not in the map', () => {
    expect(signalLabel('unknown_signal_xyz')).toBe('unknown_signal_xyz');
  });

  it('does not render raw jargon keys as chip labels', () => {
    render(<WhyChips signals={{ emb: 0.9, llm: 0.5, rec: 0.3 }} />);
    // emb/llm/rec must map to readable labels, not appear verbatim
    expect(screen.queryByText(/^emb/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^llm/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^rec/)).not.toBeInTheDocument();
  });
});

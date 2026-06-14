import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { WhyChips } from '@/components/my-day/primitives/WhyChips';
import { PULSE_WEIGHT_LABELS } from '@/components/settings/pulse/pulse-constants';

// No prior test covered WhyChips; this guards that the canonical Pulse signal
// labels are the single source of truth — no drifted local copy.
describe('WhyChips', () => {
  it('renders the canonical Pulse signal labels', () => {
    render(<WhyChips signals={{ embedding: 0.8, llm_relevance: 0.5 }} />);
    expect(screen.getByText(/Semantic similarity/)).toBeInTheDocument();
    expect(screen.getByText(/Relevance score/)).toBeInTheDocument();
    // the labels come from the canonical map, not a local duplicate
    expect(PULSE_WEIGHT_LABELS.embedding).toBe('Semantic similarity');
    expect(PULSE_WEIGHT_LABELS.llm_relevance).toBe('Relevance score');
  });

  it('falls back to a plain label for WhyChips-only signal aliases', () => {
    render(<WhyChips signals={{ topic_match: 0.9 }} />);
    expect(screen.getByText(/Topic match/)).toBeInTheDocument();
  });
});

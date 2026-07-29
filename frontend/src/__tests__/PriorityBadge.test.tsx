import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import type { PriorityLevel } from '@/types';

const PRIORITY_CASES = [
  { level: 'unscored', label: 'Not yet ranked' },
  { level: 'must-read', label: 'Must read' },
  { level: 'recommended', label: 'Recommended' },
  { level: 'background', label: 'Background' },
] satisfies ReadonlyArray<{ level: PriorityLevel; label: string }>;

describe('PriorityBadge', () => {
  it.each(PRIORITY_CASES)('renders "$label" for $level papers', ({ level, label }) => {
    render(<PriorityBadge level={level} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it('wraps the unscored badge in a Tooltip trigger (data-state attribute present)', () => {
    render(<PriorityBadge level="unscored" />);
    // Radix Tooltip adds data-state="closed" to the trigger element
    const badge = screen.getByText('Not yet ranked');
    expect(badge).toHaveAttribute('data-state', 'closed');
  });
});

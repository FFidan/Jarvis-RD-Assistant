import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PriorityBadge } from '@/components/paper/PriorityBadge';

describe('PriorityBadge', () => {
  it('renders "Not yet ranked" for unscored papers', () => {
    render(<PriorityBadge level="unscored" />);
    expect(screen.getByText('Not yet ranked')).toBeInTheDocument();
  });

  it('wraps the unscored badge in a Tooltip trigger (data-state attribute present)', () => {
    render(<PriorityBadge level="unscored" />);
    // Radix Tooltip adds data-state="closed" to the trigger element
    const badge = screen.getByText('Not yet ranked');
    expect(badge).toHaveAttribute('data-state', 'closed');
  });

  it('renders "MUST READ" for must-read level', () => {
    render(<PriorityBadge level="must-read" />);
    expect(screen.getByText('MUST READ')).toBeInTheDocument();
  });

  it('renders "Recommended" for recommended level', () => {
    render(<PriorityBadge level="recommended" />);
    expect(screen.getByText('Recommended')).toBeInTheDocument();
  });

  it('renders "Background" for background level', () => {
    render(<PriorityBadge level="background" />);
    expect(screen.getByText('Background')).toBeInTheDocument();
  });
});

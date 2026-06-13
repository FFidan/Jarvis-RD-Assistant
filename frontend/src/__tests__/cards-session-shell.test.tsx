/**
 * Unit tests for SessionShell helpers and components.
 * Covers: progress bar, breadcrumb, last-seen computation, deck-name resolution.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import {
  SessionBreadcrumb,
  SessionProgressBar,
  computeLastSeenDays,
  resolveDeckName,
} from '@/components/cards/SessionShell';
import type { Deck } from '@/types';

// --- computeLastSeenDays ---

describe('computeLastSeenDays', () => {
  it('returns null for null input', () => {
    expect(computeLastSeenDays(null)).toBeNull();
  });

  it('returns 0 for very recent updated_at (same day)', () => {
    const recent = new Date(Date.now() - 1000 * 60).toISOString(); // 1 minute ago
    expect(computeLastSeenDays(recent)).toBe(0);
  });

  it('returns 4 for ~4 days ago', () => {
    const fourDaysAgo = new Date(Date.now() - 4 * 24 * 60 * 60 * 1000).toISOString();
    expect(computeLastSeenDays(fourDaysAgo)).toBe(4);
  });

  it('does not return negative values', () => {
    // Future timestamp
    const future = new Date(Date.now() + 10_000).toISOString();
    expect(computeLastSeenDays(future)).toBe(0);
  });
});

// --- resolveDeckName ---

describe('resolveDeckName', () => {
  const decks: Deck[] = [
    { id: 1, name: 'RGS Thesis', description: null, topic_id: null, card_count: 5, due_count: 2, created_at: '' },
    { id: 2, name: 'Neural ODEs', description: null, topic_id: null, card_count: 3, due_count: 0, created_at: '' },
  ];

  it('returns null when deckId is null', () => {
    expect(resolveDeckName(decks, null)).toBeNull();
  });

  it('resolves a known deck name', () => {
    expect(resolveDeckName(decks, 1)).toBe('RGS Thesis');
  });

  it('returns null for unknown deckId', () => {
    expect(resolveDeckName(decks, 99)).toBeNull();
  });
});

// --- SessionProgressBar ---

describe('SessionProgressBar', () => {
  it('renders PROGRESS label', () => {
    render(<SessionProgressBar reviewed={0} total={10} />);
    expect(screen.getByText(/progress/i)).toBeInTheDocument();
  });

  it('displays reviewed/total counter', () => {
    render(<SessionProgressBar reviewed={3} total={12} />);
    expect(screen.getByText('3 / 12')).toBeInTheDocument();
  });

  it('sets aria-valuenow on the progress bar', () => {
    render(<SessionProgressBar reviewed={5} total={10} />);
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '5');
    expect(bar).toHaveAttribute('aria-valuemax', '10');
  });

  it('shows 0/— when total is 0', () => {
    render(<SessionProgressBar reviewed={0} total={0} />);
    expect(screen.getByText('0 / —')).toBeInTheDocument();
  });

  it('clamps progress to 100%', () => {
    render(<SessionProgressBar reviewed={15} total={10} />);
    const bar = screen.getByRole('progressbar');
    // style width should be 100%
    expect(bar).toHaveStyle({ width: '100%' });
  });
});

// --- SessionBreadcrumb ---

describe('SessionBreadcrumb', () => {
  function renderCrumb(deckName: string | null, onNav = () => {}) {
    return render(
      <MemoryRouter>
        <SessionBreadcrumb deckName={deckName} onNavigateToLibrary={onNav} />
      </MemoryRouter>,
    );
  }

  it('shows Learn and Flashcards nodes', () => {
    renderCrumb(null);
    expect(screen.getByText('Learn')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /flashcards/i })).toBeInTheDocument();
  });

  it('shows deck name with · session suffix', () => {
    renderCrumb('RGS Thesis');
    expect(screen.getByText('RGS Thesis · session')).toBeInTheDocument();
  });

  it('shows "All decks · session" when no deck selected', () => {
    renderCrumb(null);
    expect(screen.getByText('All decks · session')).toBeInTheDocument();
  });

  it('calls onNavigateToLibrary when Flashcards button clicked', async () => {
    const spy = vi.fn();
    renderCrumb(null, spy);
    await userEvent.click(screen.getByRole('button', { name: /flashcards/i }));
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

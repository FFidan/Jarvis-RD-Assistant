/**
 * KpiBand unit tests — verifies:
 *  - correct totals rendered
 *  - positive delta → green "+N vs prev"
 *  - negative delta → amber "−N vs prev"
 *  - equal delta → neutral "= vs prev"
 *  - streak badge shown when cards_review_streak_days > 0
 *  - streak badge absent (falls back to trend chip) when streak = 0
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { KpiBand } from '@/components/analytics/KpiBand';
import type { AnalyticsSummaryResponse } from '@/types';

const BASE: AnalyticsSummaryResponse = {
  papers_read_total: 24,
  focus_hours_total: 37.2,
  cards_reviewed_total: 412,
  papers_read_prev: 18,
  focus_hours_prev: 41.3,
  cards_reviewed_prev: 380,
  focus_streak_days: 5,
  cards_review_streak_days: 28,
};

function renderBand(overrides: Partial<AnalyticsSummaryResponse> = {}) {
  return render(<KpiBand data={{ ...BASE, ...overrides }} />);
}

describe('KpiBand', () => {
  it('renders all three KPI labels', () => {
    renderBand();
    expect(screen.getByText('PAPERS READ')).toBeInTheDocument();
    expect(screen.getByText('FOCUS HOURS')).toBeInTheDocument();
    expect(screen.getByText('CARDS REVIEWED')).toBeInTheDocument();
  });

  it.each([
    ['papers_read_total', '24'],
    ['focus_hours_total (1dp when non-integer)', '37.2'],
    ['cards_reviewed_total', '412'],
  ])('renders %s value', (_label, expected) => {
    renderBand();
    const values = screen.getAllByTestId('kpi-value');
    expect(values.some((el) => el.textContent === expected)).toBe(true);
  });

  // ── Trend chips ─────────────────────────────────────────────────────────

  it('positive papers delta shows +N vs prev', () => {
    // papers_read_total 24 − prev 18 = +6
    renderBand();
    const chips = screen.getAllByTestId('trend-chip');
    expect(chips.some((el) => el.textContent?.includes('+6'))).toBe(true);
  });

  it('negative focus delta shows −N vs prev', () => {
    // focus 37.2 − 41.3 = −4.1
    renderBand();
    const chips = screen.getAllByTestId('trend-chip');
    expect(chips.some((el) => el.textContent?.includes('-4.1') || el.textContent?.includes('−4.1'))).toBe(true);
  });

  it('neutral delta (equal current/prev) shows "= vs prev"', () => {
    renderBand({ papers_read_total: 10, papers_read_prev: 10 });
    expect(screen.getAllByText('= vs prev').length).toBeGreaterThan(0);
  });

  // ── Streak chip ─────────────────────────────────────────────────────────

  it('renders streak chip when cards_review_streak_days > 0', () => {
    renderBand({ cards_review_streak_days: 28 });
    const chips = screen.getAllByTestId('streak-chip');
    expect(chips.length).toBeGreaterThan(0);
    expect(chips[0]?.textContent).toContain('28-day streak');
  });

  it('renders trend chip (not streak) when cards_review_streak_days = 0', () => {
    renderBand({ cards_review_streak_days: 0, cards_reviewed_total: 100, cards_reviewed_prev: 80 });
    expect(screen.queryAllByTestId('streak-chip')).toHaveLength(0);
    // Should show a trend chip instead (delta = +20)
    const chips = screen.getAllByTestId('trend-chip');
    expect(chips.some((el) => el.textContent?.includes('+20'))).toBe(true);
  });

  it('renders 28-day streak text in CARDS REVIEWED cell', () => {
    renderBand({ cards_review_streak_days: 28 });
    expect(screen.getByTestId('streak-chip').textContent ?? '').toContain('28-day streak');
  });

  // ── Integer focus hours renders without decimal ─────────────────────────

  it('renders integer focus hours without decimal point', () => {
    renderBand({ focus_hours_total: 40, focus_hours_prev: 38 });
    const values = screen.getAllByTestId('kpi-value');
    expect(values.some((el) => el.textContent === '40')).toBe(true);
  });
});

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SourceTimeline } from '@/components/pulse/SourceTimeline';
import type { SourceRunRecord } from '@/types';

function makeRun(overrides: Partial<SourceRunRecord> = {}): SourceRunRecord {
  return {
    source_type: 'arxiv',
    started_at: '2026-05-01T04:00:00Z',
    finished_at: '2026-05-01T04:01:00Z',
    status: 'ok',
    candidate_count: 12,
    duration_ms: 60_000,
    ...overrides,
  };
}

describe('SourceTimeline', () => {
  it('renders one dot per run', () => {
    const runs: SourceRunRecord[] = [
      makeRun({ started_at: '2026-05-01T04:00:00Z' }),
      makeRun({ started_at: '2026-05-02T04:00:00Z' }),
      makeRun({ started_at: '2026-05-03T04:00:00Z' }),
    ];
    render(<SourceTimeline sourceType="arxiv" runs={runs} />);
    const dots = screen.getAllByTestId('run-dot-ok');
    expect(dots).toHaveLength(3);
  });

  it('uses green for ok status', () => {
    const runs = [makeRun({ status: 'ok' })];
    render(<SourceTimeline sourceType="arxiv" runs={runs} />);
    const dot = screen.getByTestId('run-dot-ok');
    expect(dot.className).toContain('bg-green-500');
  });

  it('uses yellow for rate_limit status', () => {
    const runs = [makeRun({ status: 'rate_limit' })];
    render(<SourceTimeline sourceType="arxiv" runs={runs} />);
    const dot = screen.getByTestId('run-dot-rate_limit');
    expect(dot.className).toContain('bg-yellow-400');
  });

  it('uses red for error status', () => {
    const runs = [makeRun({ status: 'error' })];
    render(<SourceTimeline sourceType="arxiv" runs={runs} />);
    const dot = screen.getByTestId('run-dot-error');
    expect(dot.className).toContain('bg-red-500');
  });

  it('uses gray for cooldown_skip status', () => {
    const runs = [makeRun({ status: 'cooldown_skip' })];
    render(<SourceTimeline sourceType="arxiv" runs={runs} />);
    const dot = screen.getByTestId('run-dot-cooldown_skip');
    expect(dot.className).toContain('bg-gray-400');
  });

  it('shows "No runs in window" when runs array is empty', () => {
    render(<SourceTimeline sourceType="arxiv" runs={[]} />);
    expect(screen.getByText(/no runs in window/i)).toBeInTheDocument();
  });

  it('renders the source type label', () => {
    render(
      <SourceTimeline
        sourceType="semantic_scholar"
        runs={[makeRun({ source_type: 'semantic_scholar' })]}
      />,
    );
    expect(screen.getByText('semantic_scholar')).toBeInTheDocument();
  });

  it('renders mixed statuses with correct colors', () => {
    const runs: SourceRunRecord[] = [
      makeRun({ started_at: '2026-05-01T04:00:00Z', status: 'ok' }),
      makeRun({ started_at: '2026-05-02T04:00:00Z', status: 'error' }),
      makeRun({ started_at: '2026-05-03T04:00:00Z', status: 'rate_limit' }),
    ];
    render(<SourceTimeline sourceType="arxiv" runs={runs} />);
    expect(screen.getByTestId('run-dot-ok').className).toContain('bg-green-500');
    expect(screen.getByTestId('run-dot-error').className).toContain('bg-red-500');
    expect(screen.getByTestId('run-dot-rate_limit').className).toContain('bg-yellow-400');
  });
});

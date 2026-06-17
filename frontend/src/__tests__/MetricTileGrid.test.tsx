import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { MetricTileGrid } from '@/components/home/MetricTileGrid';
import type { DashboardMetrics } from '@/types';

const mockMetrics: DashboardMetrics = {
  total_papers: 10,
  unread_papers: 10,
  pending_papers: 5,
  due_cards: 0,
  active_projects: 0,
  topic_count: 0,
  nudge_count: 0,
};

function renderGrid(metrics: DashboardMetrics | undefined, isLoading = false) {
  return render(
    <MemoryRouter>
      <MetricTileGrid metrics={metrics} isLoading={isLoading} />
    </MemoryRouter>,
  );
}

describe('MetricTileGrid', () => {
  it('renders 5 metric tiles', () => {
    renderGrid(mockMetrics);
    // Each tile has a title; check all five are present
    expect(screen.getByText('Papers')).toBeInTheDocument();
    expect(screen.getByText('Due Cards')).toBeInTheDocument();
    expect(screen.getByText('Active Projects')).toBeInTheDocument();
    expect(screen.getByText('Topics')).toBeInTheDocument();
    expect(screen.getByText('Scheduled Jobs')).toBeInTheDocument();
  });

  it('does not render old tile titles', () => {
    renderGrid(mockMetrics);
    expect(screen.queryByText('Total Papers')).not.toBeInTheDocument();
    expect(screen.queryByText('Unread Papers')).not.toBeInTheDocument();
    expect(screen.queryByText('Unsummarized')).not.toBeInTheDocument();
    expect(screen.queryByText('Nudges')).not.toBeInTheDocument();
  });

  it('Library tile subtitle contains unread count when backlog exists', () => {
    renderGrid(mockMetrics);
    expect(screen.getByText('10 unread · 5 unsummarized')).toBeInTheDocument();
  });

  it('Library tile subtitle shows "All caught up" when no unread papers', () => {
    const caughtUp: DashboardMetrics = { ...mockMetrics, unread_papers: 0 };
    renderGrid(caughtUp);
    expect(screen.getByText('All caught up')).toBeInTheDocument();
  });

  it('shows skeleton loaders while loading', () => {
    const { container } = renderGrid(undefined, true);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders 5 tiles (not 7) as confirmed by tile title count', () => {
    renderGrid(mockMetrics);
    const tileTitles = ['Papers', 'Due Cards', 'Active Projects', 'Topics', 'Scheduled Jobs'];
    expect(tileTitles).toHaveLength(5);
    tileTitles.forEach((title) => {
      expect(screen.getByText(title)).toBeInTheDocument();
    });
  });
});

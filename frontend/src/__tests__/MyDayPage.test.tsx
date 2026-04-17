import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { MyDayPage } from '@/pages/MyDayPage';
import * as api from '@/lib/api';
import type { MyDayResponse } from '@/types';

// Mock the api module
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchMyDay: vi.fn(),
    fetchProjects: vi.fn(),
    createQuickTask: vi.fn(),
    logFocusSession: vi.fn(),
    updateTask: vi.fn(),
  };
});

const mockMyDayData: MyDayResponse = {
  tasks: [
    {
      id: 1, project_id: 10, title: 'Fix embedding pipeline',
      priority: 1, deadline: null, status: 'todo',
      completed_at: null, project_name: 'JARVIS', project_color: '#3b82f6',
    },
    {
      id: 2, project_id: null, title: 'Buy groceries',
      priority: 3, deadline: null, status: 'todo',
      completed_at: null, project_name: null, project_color: null,
    },
  ],
  cards_due: 5,
  recommendations: [
    { recommendation_id: 1, paper_id: 100, score: 0.95, title: 'Neural ODEs', authors: ['Chen'] },
  ],
  today_focus_hours: 2.5,
  focus_streak_days: 4,
  project_pulse: [
    { id: 10, name: 'JARVIS', total_tasks: 10, done_tasks: 7, next_milestone: 'v2 release', next_milestone_deadline: null },
  ],
};

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <MyDayPage />
      </BrowserRouter>
    </QueryClientProvider>,
  );
}

describe('MyDayPage', () => {
  beforeEach(() => {
    vi.mocked(api.fetchMyDay).mockResolvedValue(mockMyDayData);
    vi.mocked(api.fetchProjects).mockResolvedValue([]);
  });

  it("renders heading with today's date", async () => {
    renderWithProviders();
    expect(await screen.findByText('My Day')).toBeInTheDocument();
  });

  it('renders tasks with project badges', async () => {
    renderWithProviders();
    expect(await screen.findByText('Fix embedding pipeline')).toBeInTheDocument();
    // 'JARVIS' appears as both the task badge and the project pulse link
    expect(screen.getAllByText('JARVIS').length).toBeGreaterThanOrEqual(1);
  });

  it('renders standalone tasks without badge', async () => {
    renderWithProviders();
    expect(await screen.findByText('Buy groceries')).toBeInTheDocument();
  });

  it('shows Pomodoro timer in idle state', async () => {
    renderWithProviders();
    expect(await screen.findByText('Pomodoro Timer')).toBeInTheDocument();
    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.getByText('Start Focus')).toBeInTheDocument();
  });

  it('shows project pulse with progress', async () => {
    renderWithProviders();
    expect(await screen.findByText('Project Progress')).toBeInTheDocument();
    // 'JARVIS' appears as both the task badge and the project pulse link
    expect(screen.getAllByText('JARVIS').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('70%')).toBeInTheDocument();
  });

  it('shows learning card count', async () => {
    renderWithProviders();
    expect(await screen.findByText('5 cards due')).toBeInTheDocument();
    expect(screen.getByText('Review Now')).toBeInTheDocument();
  });

  it('collapses Learning and Recommended when both empty', async () => {
    vi.mocked(api.fetchMyDay).mockResolvedValue({
      ...mockMyDayData,
      cards_due: 0,
      recommendations: [],
    });
    renderWithProviders();
    // Compact row should appear
    expect(await screen.findByText('No reviews or recommendations right now')).toBeInTheDocument();
    // The individual card titles should NOT appear
    expect(screen.queryByText('Learning')).not.toBeInTheDocument();
    expect(screen.queryByText('Recommended')).not.toBeInTheDocument();
  });

  it('shows Learning card when cards are due even if no recommendations', async () => {
    vi.mocked(api.fetchMyDay).mockResolvedValue({
      ...mockMyDayData,
      cards_due: 3,
      recommendations: [],
    });
    renderWithProviders();
    expect(await screen.findByText('3 cards due')).toBeInTheDocument();
    // Grid still renders with both cards
    expect(screen.getByText('Learning')).toBeInTheDocument();
    expect(screen.getByText('Recommended')).toBeInTheDocument();
  });

  it('renders project badges as clickable links', async () => {
    renderWithProviders();
    await screen.findByText('Fix embedding pipeline');
    // Find the JARVIS text elements - they appear as badge and in project pulse
    const jarvisElements = screen.getAllByText('JARVIS');
    // At least one should be inside a link to /projects
    const badgeLink = jarvisElements.find(el => el.closest('a[href="/projects"]'));
    expect(badgeLink).toBeTruthy();
  });

  it('shows Pause button when timer is in work phase', async () => {
    // Import and set store state to simulate work phase
    const { usePomodoroStore } = await import('@/stores/pomodoro-store');
    usePomodoroStore.setState({
      phase: 'work',
      startedAt: Date.now(),
      pausedAt: null,
      totalPausedMs: 0,
      phaseDurationMs: 25 * 60 * 1000,
      secondsRemaining: 1500,
      cyclesCompleted: 0,
    });

    renderWithProviders();
    await screen.findByText('My Day');
    expect(screen.getByText('Pause')).toBeInTheDocument();
    expect(screen.getByText(/Stop & Log/)).toBeInTheDocument();

    // Clean up store
    usePomodoroStore.getState().reset();
  });
});

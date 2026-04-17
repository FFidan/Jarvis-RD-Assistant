import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { MyDayPage } from '@/pages/MyDayPage';
import * as api from '@/lib/api';
import type { MyDayResponse, RetentionStats, PulseDeck } from '@/types';

// Mock the api module
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchMyDay: vi.fn(),
    fetchProjects: vi.fn(),
    fetchPulseToday: vi.fn(),
    fetchFeedPapers: vi.fn(),
    getStats: vi.fn(),
    createQuickTask: vi.fn(),
    logFocusSession: vi.fn(),
    updateTask: vi.fn(),
  };
});

// Mock job store
vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      startJob: vi.fn(),
    }),
  ),
}));

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

const mockRetentionStats: RetentionStats = {
  total_cards: 50,
  due_now: 5,
  reviewed_today: 3,
  average_retention: 0.85,
  reviews_by_rating: {},
  streak_days: 7,
};

const mockPulseDeck: PulseDeck | null = null;

const mockFeedResponse = { papers: [], total: 0 };

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
    vi.mocked(api.fetchPulseToday).mockResolvedValue(mockPulseDeck);
    vi.mocked(api.fetchFeedPapers).mockResolvedValue(mockFeedResponse);
    vi.mocked(api.getStats).mockResolvedValue(mockRetentionStats);
  });

  it('renders greeting and date', async () => {
    renderWithProviders();
    // DayHeader renders greeting text (Good morning/afternoon/evening)
    const greeting = await screen.findByText(/Good (morning|afternoon|evening)/);
    expect(greeting).toBeInTheDocument();
  });

  it('renders counter strip with Pulse papers label', async () => {
    renderWithProviders();
    expect(await screen.findByText('Pulse papers')).toBeInTheDocument();
  });

  it('renders counter strip with Cards due label', async () => {
    renderWithProviders();
    expect(await screen.findByText('Cards due')).toBeInTheDocument();
  });

  it('renders counter strip with Tasks today label', async () => {
    renderWithProviders();
    expect(await screen.findByText('Tasks today')).toBeInTheDocument();
  });

  it('renders tasks from my-day data', async () => {
    renderWithProviders();
    expect(await screen.findByText('Fix embedding pipeline')).toBeInTheDocument();
    expect(screen.getByText('Buy groceries')).toBeInTheDocument();
  });

  it("renders Today's Pulse section", async () => {
    renderWithProviders();
    expect(await screen.findByText("Today's Pulse")).toBeInTheDocument();
  });

  it('renders Pomodoro timer in idle state', async () => {
    renderWithProviders();
    expect(await screen.findByText('Pomodoro Timer')).toBeInTheDocument();
    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.getByText('Start Focus')).toBeInTheDocument();
  });

  it('renders Action Items card', async () => {
    renderWithProviders();
    expect(await screen.findByText('Action Items')).toBeInTheDocument();
  });

  it('shows "all caught up" when no unprocessed papers or failed jobs', async () => {
    renderWithProviders();
    expect(await screen.findByText("You're all caught up")).toBeInTheDocument();
  });

  it('renders Learning card from LearningCardsSummary', async () => {
    renderWithProviders();
    expect(await screen.findByText('Learning')).toBeInTheDocument();
  });

  it('shows Review Now button when due_now > 0', async () => {
    renderWithProviders();
    expect(await screen.findByText('Review Now')).toBeInTheDocument();
  });

  it('shows streak days from retention stats', async () => {
    renderWithProviders();
    expect(await screen.findByText('7 day streak')).toBeInTheDocument();
  });

  it('shows project pulse', async () => {
    renderWithProviders();
    expect(await screen.findByText('Project Progress')).toBeInTheDocument();
    expect(screen.getAllByText('JARVIS').length).toBeGreaterThanOrEqual(1);
  });

  it('shows Pause button when timer is in work phase', async () => {
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
    await screen.findByText('Pomodoro Timer');
    expect(screen.getByText('Pause')).toBeInTheDocument();

    usePomodoroStore.getState().reset();
  });
});

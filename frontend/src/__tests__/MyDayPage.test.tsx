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
    fetchMissingFoundationalPapers: vi.fn(),
    fetchAndProcessFoundationalPaper: vi.fn(),
    ratePulseCard: vi.fn(),
  };
});

// Mock job store
vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      isRunning: () => false,
      startJob: vi.fn(),
      trackExternalJob: vi.fn(),
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
    vi.mocked(api.fetchMissingFoundationalPapers).mockResolvedValue([]);
  });

  it('renders DateMasthead with research log header', async () => {
    renderWithProviders();
    // DateMasthead renders "RESEARCH LOG · ENTRY…" monospace header
    expect(await screen.findByText(/RESEARCH LOG/)).toBeInTheDocument();
  });

  it('renders DateMasthead counter labels', async () => {
    renderWithProviders();
    // DateMasthead renders lowercase counter labels: pulse, due, tasks, new
    expect(await screen.findByText('pulse')).toBeInTheDocument();
    expect(screen.getByText('due')).toBeInTheDocument();
    expect(screen.getByText('tasks')).toBeInTheDocument();
    expect(screen.getByText('new')).toBeInTheDocument();
  });

  it('renders Yesterday section marker', async () => {
    renderWithProviders();
    expect(await screen.findByText(/§ Yesterday/i)).toBeInTheDocument();
  });

  it('renders Now section with hero tabs', async () => {
    renderWithProviders();
    expect(await screen.findByText(/§ Now/i)).toBeInTheDocument();
    expect(screen.getByText('Pulse #1')).toBeInTheDocument();
    expect(screen.getByText('Continue task')).toBeInTheDocument();
  });

  it("renders Today's intent section marker", async () => {
    renderWithProviders();
    expect(await screen.findByText(/§ Today's intent/i)).toBeInTheDocument();
  });

  it('renders tasks from my-day data in IntentSection', async () => {
    renderWithProviders();
    expect(await screen.findByText('Fix embedding pipeline')).toBeInTheDocument();
    expect(screen.getByText('Buy groceries')).toBeInTheDocument();
  });

  it('renders Projects section with project name', async () => {
    renderWithProviders();
    expect(await screen.findByText(/§ Projects/i)).toBeInTheDocument();
    // Wait for async data to hydrate the project list
    expect(await screen.findAllByText('JARVIS')).not.toHaveLength(0);
  });

  it('renders Learning & focus section marker', async () => {
    renderWithProviders();
    expect(await screen.findByText(/§ Learning/i)).toBeInTheDocument();
  });

  it('renders Learning cards sub-section', async () => {
    renderWithProviders();
    expect(await screen.findByText('Learning cards')).toBeInTheDocument();
  });

  it('shows Review now button when due_now > 0', async () => {
    renderWithProviders();
    expect(await screen.findByText('Review now →')).toBeInTheDocument();
  });

  it('shows streak days from retention stats', async () => {
    renderWithProviders();
    // LearningFocusSection renders "7d streak" (compact format)
    expect(await screen.findByText(/7d streak/)).toBeInTheDocument();
  });

  it('renders End of day section marker', async () => {
    renderWithProviders();
    expect(await screen.findByText(/§ End of day/i)).toBeInTheDocument();
  });

  it('does not render Triage section when no action items or foundational gaps', async () => {
    renderWithProviders();
    // Wait for data to load, then assert Triage is absent (it returns null when empty)
    await screen.findByText(/RESEARCH LOG/);
    expect(screen.queryByText(/§ Triage/i)).not.toBeInTheDocument();
  });

  it('renders no-pulse-yet message in HeroNow when deck is null', async () => {
    renderWithProviders();
    // When pulseDeck resolves to null, HeroPulse (inside HeroNow §Now) shows
    // a "No Pulse for today yet" message instead of pulse card content.
    expect(
      await screen.findByText(/No Pulse for today yet/i),
    ).toBeInTheDocument();
  });
});

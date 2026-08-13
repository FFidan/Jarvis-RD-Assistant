import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BrowserRouter, MemoryRouter } from 'react-router-dom';
import { MyDayPage } from '@/pages/MyDayPage';
import * as api from '@/lib/api';
import type { MyDayResponse, RetentionStats, PulseDeck, PulseCardItem, MyDayBundle } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// Mock the api module
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchMyDay: vi.fn(),
    fetchProjects: vi.fn(),
    fetchPulseToday: vi.fn(),
    fetchFeed: vi.fn(),
    getStats: vi.fn(),
    createQuickTask: vi.fn(),
    logFocusSession: vi.fn(),
    updateTask: vi.fn(),
    fetchMissingFoundationalPapers: vi.fn(),
    fetchAndProcessFoundationalPaper: vi.fn(),
    ratePulseCard: vi.fn(),
    fetchWeeklyDigest: vi.fn(),
    fetchYesterday: vi.fn(),
    fetchThreads: vi.fn(),
    getJournalEntry: vi.fn(),
    upsertJournalEntry: vi.fn(),
    seedThreadFromEod: vi.fn(),
    fetchIntentToday: vi.fn(),
    saveIntentToday: vi.fn(),
    getMyDayBundle: vi.fn(),
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
    { id: 10, name: 'JARVIS', color: null, total_tasks: 10, done_tasks: 7, next_milestone: 'v2 release', next_milestone_deadline: null },
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

const mockBundle: MyDayBundle = {
  tasks: [],
  intent: { intent: null, updated_at: null },
  threads: [],
  yesterday: {
    date: '2026-05-14',
    focused_hours: 0,
    cards_reviewed: 0,
    tasks_done: 0,
    completed: [],
    deferred: [],
  },
  journal: null,
};

function renderSubject() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <BrowserRouter>
      <MyDayPage />
    </BrowserRouter>,
    { queryClient },
  );
}

describe('MyDayPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchMyDay).mockResolvedValue(mockMyDayData);
    vi.mocked(api.fetchProjects).mockResolvedValue([]);
    vi.mocked(api.fetchYesterday).mockResolvedValue({
      date: '2026-05-14',
      focused_hours: 0,
      cards_reviewed: 0,
      tasks_done: 0,
      completed: [],
      deferred: [],
    });
    vi.mocked(api.fetchThreads).mockResolvedValue([]);
    vi.mocked(api.getJournalEntry).mockResolvedValue(null);
    vi.mocked(api.upsertJournalEntry).mockResolvedValue({
      id: 1,
      date: '2026-05-15',
      prompts: {},
      created_at: '2026-05-15T00:00:00Z',
      updated_at: '2026-05-15T00:00:00Z',
    });
    vi.mocked(api.fetchIntentToday).mockResolvedValue({ intent: null, updated_at: null });
    vi.mocked(api.fetchPulseToday).mockResolvedValue(mockPulseDeck);
    vi.mocked(api.fetchFeed).mockResolvedValue(mockFeedResponse);
    vi.mocked(api.getStats).mockResolvedValue(mockRetentionStats);
    vi.mocked(api.fetchMissingFoundationalPapers).mockResolvedValue([]);
    vi.mocked(api.fetchWeeklyDigest).mockResolvedValue({
      topics: [],
      total_papers: 0,
      period_start: '2026-05-01T00:00:00Z',
      period_end: '2026-05-08T00:00:00Z',
    });
    vi.mocked(api.getMyDayBundle).mockResolvedValue(mockBundle);
  });

  it('renders DateMasthead with research log header', async () => {
    renderSubject();
    // DateMasthead renders "RESEARCH LOG · ENTRY…" monospace header
    expect(await screen.findByText(/RESEARCH LOG/)).toBeInTheDocument();
  });

  it('renders DateMasthead counter labels', async () => {
    renderSubject();
    // DateMasthead renders lowercase counter labels: pulse, due, tasks, new
    expect(await screen.findByText('pulse')).toBeInTheDocument();
    expect(screen.getByText('due')).toBeInTheDocument();
    expect(screen.getByText('tasks')).toBeInTheDocument();
    expect(screen.getByText('new')).toBeInTheDocument();
  });

  it('hides Yesterday section when there was no recorded activity', async () => {
    renderSubject();
    await screen.findByText(/RESEARCH LOG/);
    // YesterdaySection is an on-the-fly rollup; it stays silent when
    // completed+deferred are both empty (default mock).
    expect(screen.queryByText(/Yesterday/i)).not.toBeInTheDocument();
  });

  it('renders Yesterday section when the rollup has activity', async () => {
    vi.mocked(api.fetchYesterday).mockResolvedValue({
      date: '2026-05-14',
      focused_hours: 2.5,
      cards_reviewed: 4,
      tasks_done: 1,
      completed: [{ id: 11, title: 'Closed the solver benchmark', status: 'done' }],
      deferred: [{ id: 12, title: 'Adjoint proof', status: 'todo' }],
    });
    renderSubject();
    expect(await screen.findByText(/Yesterday/i)).toBeInTheDocument();
    expect(screen.getByText('Closed the solver benchmark')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /carry over/i })).toBeInTheDocument();
  });

  it('renders Now section with Pulse #1 mode tab', async () => {
    renderSubject();
    expect(await screen.findByText(/Now/i)).toBeInTheDocument();
    // ModePicker uses role="tab" buttons (ARIA enhancement); Pulse #1 is always visible
    expect(screen.getByRole('tab', { name: 'Pulse #1' })).toBeInTheDocument();
    // Continue task only shows with an active Pomodoro (none in this test)
  });

  it("renders Today's intent section marker", async () => {
    renderSubject();
    expect(await screen.findByText(/Today's intent/i)).toBeInTheDocument();
  });

  it('renders tasks from my-day data in IntentSection', async () => {
    renderSubject();
    expect(await screen.findByText('Fix embedding pipeline')).toBeInTheDocument();
    expect(screen.getByText('Buy groceries')).toBeInTheDocument();
  });

  it('renders Projects section with project name', async () => {
    renderSubject();
    expect(await screen.findByText(/Projects/i)).toBeInTheDocument();
    // Wait for async data to hydrate the project list
    expect(await screen.findAllByText('JARVIS')).not.toHaveLength(0);
  });

  it('renders Learning & focus section marker', async () => {
    renderSubject();
    expect(await screen.findByText(/Learning & focus/i)).toBeInTheDocument();
  });

  it('renders Learning cards sub-section', async () => {
    renderSubject();
    expect(await screen.findByText('Learning cards')).toBeInTheDocument();
  });

  it('shows Review now button when due_now > 0', async () => {
    renderSubject();
    expect(await screen.findByText('Review now →')).toBeInTheDocument();
  });

  it('shows streak days from retention stats', async () => {
    renderSubject();
    // LearningFocusSection renders "7d streak" (compact format)
    expect(await screen.findByText(/7d streak/)).toBeInTheDocument();
  });

  it('renders End of day shutdown ritual with the 3 structured prompts', async () => {
    renderSubject();
    expect(await screen.findByText(/End of day/i)).toBeInTheDocument();
    expect(screen.getByLabelText('One thing that worked')).toBeInTheDocument();
    expect(screen.getByLabelText("What's still blocking me")).toBeInTheDocument();
    expect(screen.getByLabelText('First move tomorrow')).toBeInTheDocument();
  });

  it('does not render Triage section when no action items or foundational gaps', async () => {
    renderSubject();
    // Wait for data to load, then assert Triage is absent (it returns null when empty)
    await screen.findByText(/RESEARCH LOG/);
    expect(screen.queryByText(/Triage/i)).not.toBeInTheDocument();
  });

  it('renders no-pulse-yet message in HeroNow when deck is null', async () => {
    renderSubject();
    // When pulseDeck resolves to null, HeroPulse (inside HeroNow Now) shows
    // a "No Pulse for today yet" message instead of pulse card content.
    expect(
      await screen.findByText(/No Pulse for today yet/i),
    ).toBeInTheDocument();
  });

  it('calls getMyDayBundle once on mount (F7 bundle single round-trip)', async () => {
    // F7: one bundle call replaces ~4 per-section cold fetches. Verify the
    // bundle is fetched exactly once and the page renders correctly.
    renderSubject();
    await screen.findByText(/RESEARCH LOG/);
    await waitFor(() => {
      expect(api.getMyDayBundle).toHaveBeenCalledTimes(1);
    });
  });

  it('FE-UIA-03: "all projects →" link uses React Router (no full-page reload)', async () => {
    renderSubject();
    // Wait for project section to render
    expect(await screen.findAllByText('JARVIS')).not.toHaveLength(0);
    // The link must be a <a> element pointing to /projects but rendered via
    // React Router's <Link>, which means no href-only navigation.
    const link = screen.getByRole('link', { name: /all projects/i });
    expect(link).toBeInTheDocument();
    // React Router Link renders <a> with href="/projects"
    expect(link).toHaveAttribute('href', '/projects');
  });

  it('FE-UIA-04: does not render the hardcoded epoch entry-number footer', async () => {
    renderSubject();
    await screen.findByText(/RESEARCH LOG/);
    // The "end of entry N" footer should no longer appear since the epoch-based
    // calculation was removed and MyDayFooter is no longer rendered.
    expect(screen.queryByText(/end of entry/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// HeroPulse-specific behaviour (B4)
// ---------------------------------------------------------------------------

function makePulseCard(overrides: Partial<PulseCardItem> = {}): PulseCardItem {
  return {
    card_id: 1,
    paper_id: 101,
    paper_title: 'Attention Is All You Need',
    paper_authors: ['Vaswani et al.'],
    paper_url: null,
    rank: 1,
    score: 0.9,
    llm_relevance: null,
    llm_novelty: null,
    reasoning: null,
    reasoning_verified: null,
    reasoning_confidence: null,
    signals: {},
    user_state: null,
    tags: null,
    ...overrides,
  };
}

function makeDeck(cards: PulseCardItem[]): PulseDeck {
  return {
    deck_id: 1,
    deck_date: '2026-05-03',
    card_count: cards.length,
    generated_at: '2026-05-03T08:00:00Z',
    cards,
    stats: {},
    degraded_reason: null,
  };
}

describe('HeroPulse behaviour', () => {
  beforeEach(() => {
    vi.mocked(api.fetchMyDay).mockResolvedValue(mockMyDayData);
    vi.mocked(api.fetchProjects).mockResolvedValue([]);
    vi.mocked(api.fetchYesterday).mockResolvedValue({
      date: '2026-05-14',
      focused_hours: 0,
      cards_reviewed: 0,
      tasks_done: 0,
      completed: [],
      deferred: [],
    });
    vi.mocked(api.fetchThreads).mockResolvedValue([]);
    vi.mocked(api.getJournalEntry).mockResolvedValue(null);
    vi.mocked(api.upsertJournalEntry).mockResolvedValue({
      id: 1,
      date: '2026-05-15',
      prompts: {},
      created_at: '2026-05-15T00:00:00Z',
      updated_at: '2026-05-15T00:00:00Z',
    });
    vi.mocked(api.fetchIntentToday).mockResolvedValue({ intent: null, updated_at: null });
    vi.mocked(api.fetchFeed).mockResolvedValue(mockFeedResponse);
    vi.mocked(api.getStats).mockResolvedValue(mockRetentionStats);
    vi.mocked(api.fetchMissingFoundationalPapers).mockResolvedValue([]);
    vi.mocked(api.fetchWeeklyDigest).mockResolvedValue({
      topics: [],
      total_papers: 0,
      period_start: '2026-05-01T00:00:00Z',
      period_end: '2026-05-08T00:00:00Z',
    });
    vi.mocked(api.getMyDayBundle).mockResolvedValue(mockBundle);
  });

  it('two-card deck shows #1 of 2 in the meta text initially', async () => {
    const cards = [
      makePulseCard({ card_id: 1, paper_id: 101, paper_title: 'Paper One', rank: 1 }),
      makePulseCard({ card_id: 2, paper_id: 102, paper_title: 'Paper Two', rank: 2 }),
    ];
    vi.mocked(api.fetchPulseToday).mockResolvedValue(makeDeck(cards));

    renderSubject();

    // HeroPulse meta line: "Triage today's pulse · ~6 min · #1 of 2"
    expect(await screen.findByText(/#1 of 2/)).toBeInTheDocument();
  });

  it('after Accept mutation resolves, advances to #2 of 2', async () => {
    const user = userEvent.setup();

    const cards = [
      makePulseCard({ card_id: 1, paper_id: 101, paper_title: 'Paper One', rank: 1 }),
      makePulseCard({ card_id: 2, paper_id: 102, paper_title: 'Paper Two', rank: 2 }),
    ];
    vi.mocked(api.fetchPulseToday).mockResolvedValue(makeDeck(cards));
    // ratePulseCard resolves immediately; onSuccess increments currentIndex
    vi.mocked(api.ratePulseCard).mockResolvedValue(undefined as any);

    renderSubject();

    // Wait for initial render
    expect(await screen.findByText(/#1 of 2/)).toBeInTheDocument();

    // Click Accept (rating: 'up')
    const acceptBtn = screen.getByRole('button', { name: 'Accept' });
    await user.click(acceptBtn);

    // After mutation onSuccess, currentIndex increments to 1 → "#2 of 2"
    expect(await screen.findByText(/#2 of 2/)).toBeInTheDocument();
  });

  it('shows cleared state text when all cards in a 1-card deck are rated', async () => {
    const user = userEvent.setup();

    const cards = [
      makePulseCard({ card_id: 1, paper_id: 101, paper_title: 'Solo Paper', rank: 1 }),
    ];
    vi.mocked(api.fetchPulseToday).mockResolvedValue(makeDeck(cards));
    vi.mocked(api.ratePulseCard).mockResolvedValue(undefined as any);

    renderSubject();

    // Wait for the card to appear
    expect(await screen.findByText(/#1 of 1/)).toBeInTheDocument();

    // Rate it (Skip or Accept both increment currentIndex via onSuccess)
    const skipBtn = screen.getByRole('button', { name: 'Skip' });
    await user.click(skipBtn);

    // After rating the only card, cleared state should appear
    expect(
      await screen.findByText(/All caught up/i),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Hash-scroll behaviour
// ---------------------------------------------------------------------------

describe('MyDayPage hash-scroll', () => {
  beforeEach(() => {
    vi.mocked(api.fetchMyDay).mockResolvedValue(mockMyDayData);
    vi.mocked(api.fetchProjects).mockResolvedValue([]);
    vi.mocked(api.fetchYesterday).mockResolvedValue({
      date: '2026-05-14',
      focused_hours: 0,
      cards_reviewed: 0,
      tasks_done: 0,
      completed: [],
      deferred: [],
    });
    vi.mocked(api.fetchThreads).mockResolvedValue([]);
    vi.mocked(api.getJournalEntry).mockResolvedValue(null);
    vi.mocked(api.upsertJournalEntry).mockResolvedValue({
      id: 1,
      date: '2026-05-15',
      prompts: {},
      created_at: '2026-05-15T00:00:00Z',
      updated_at: '2026-05-15T00:00:00Z',
    });
    vi.mocked(api.fetchIntentToday).mockResolvedValue({ intent: null, updated_at: null });
    vi.mocked(api.fetchPulseToday).mockResolvedValue(mockPulseDeck);
    vi.mocked(api.fetchFeed).mockResolvedValue(mockFeedResponse);
    vi.mocked(api.getStats).mockResolvedValue(mockRetentionStats);
    vi.mocked(api.fetchMissingFoundationalPapers).mockResolvedValue([]);
    vi.mocked(api.fetchWeeklyDigest).mockResolvedValue({
      topics: [],
      total_papers: 0,
      period_start: '2026-05-01T00:00:00Z',
      period_end: '2026-05-08T00:00:00Z',
    });
    vi.mocked(api.getMyDayBundle).mockResolvedValue(mockBundle);
  });

  it('scrolls to #now section via rAF retry loop when element appears after mount', async () => {
    // jsdom does not implement scrollIntoView — stub it so we can assert it was called.
    const scrollIntoViewMock = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoViewMock;

    const queryClient = createTestQueryClient();

    renderWithProviders(
      <MemoryRouter initialEntries={['/my-day#now']}>
        <MyDayPage />
      </MemoryRouter>,
      { queryClient },
    );

    // HeroNow renders <section id="now"> — wait for it to be present in the DOM.
    // The Now marker text is rendered inside the section by SectionHeader.
    await screen.findByText(/Now/i);

    // The rAF retry loop should find #now and call scrollIntoView with the
    // smooth options specified in MyDayPage's useEffect.
    await vi.waitFor(() => {
      expect(scrollIntoViewMock).toHaveBeenCalledWith({
        behavior: 'smooth',
        block: 'start',
      });
    });
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { DayHeader } from '@/components/my-day/DayHeader';
import * as api from '@/lib/api';
import type { MyDayResponse, PulseDeck } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchMyDay: vi.fn(),
    fetchPulseToday: vi.fn(),
    fetchFeedPapers: vi.fn(),
  };
});

const mockMyDay: MyDayResponse = {
  tasks: [
    { id: 1, project_id: null, title: 'Task A', priority: 1, deadline: null, status: 'todo', completed_at: null, project_name: null, project_color: null },
  ],
  cards_due: 3,
  recommendations: [],
  today_focus_hours: 1,
  focus_streak_days: 2,
  project_pulse: [],
};

const mockPulseDeck: Partial<PulseDeck> = {
  deck_id: 1,
  card_count: 7,
  generated_at: new Date().toISOString(),
  cards: [],
  stats: {},
};

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <DayHeader />
      </BrowserRouter>
    </QueryClientProvider>,
  );
}

describe('DayHeader', () => {
  beforeEach(() => {
    vi.mocked(api.fetchMyDay).mockResolvedValue(mockMyDay);
    vi.mocked(api.fetchPulseToday).mockResolvedValue(mockPulseDeck as PulseDeck);
    vi.mocked(api.fetchFeedPapers).mockResolvedValue({ papers: [], total: 0 });
  });

  it('renders greeting', async () => {
    renderWithProviders();
    const greeting = await screen.findByText(/Good (morning|afternoon|evening)/);
    expect(greeting).toBeInTheDocument();
  });

  it('renders date label', async () => {
    renderWithProviders();
    // Date formatted as "Monday, Apr 17" etc.
    const date = await screen.findByText(/\w+, \w+ \d+/);
    expect(date).toBeInTheDocument();
  });

  it('renders Pulse papers counter', async () => {
    renderWithProviders();
    expect(await screen.findByText('Pulse papers')).toBeInTheDocument();
    // Pulse count from deck.card_count = 7
    expect(await screen.findByText('7')).toBeInTheDocument();
  });

  it('renders Cards due counter with link', async () => {
    renderWithProviders();
    expect(await screen.findByText('Cards due')).toBeInTheDocument();
    expect(await screen.findByText('3')).toBeInTheDocument();
  });

  it('renders Tasks today counter', async () => {
    renderWithProviders();
    expect(await screen.findByText('Tasks today')).toBeInTheDocument();
  });

  it('renders Unprocessed uploads counter', async () => {
    renderWithProviders();
    expect(await screen.findByText('Unprocessed uploads')).toBeInTheDocument();
    // 0 unprocessed from mock
    const zeros = await screen.findAllByText('0');
    expect(zeros.length).toBeGreaterThanOrEqual(1);
  });

  it('renders skeleton while loading', () => {
    vi.mocked(api.fetchMyDay).mockReturnValue(new Promise(() => {}));
    renderWithProviders();
    // Skeletons use animate-pulse class (shadcn Skeleton component)
    const skeletons = document.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });
});

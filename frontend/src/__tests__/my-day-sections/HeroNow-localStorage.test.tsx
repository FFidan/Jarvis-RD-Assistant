import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HeroNow } from '@/components/my-day/sections/HeroNow';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const startWorkMock = vi.fn();

vi.mock('@/stores/pomodoro-store', () => ({
  usePomodoroStore: Object.assign(
    (selector?: (state: any) => any) => {
      const state = { phase: 'idle', attachedItem: null, pausedAt: null };
      return selector ? selector(state) : state;
    },
    { getState: () => ({ startWork: startWorkMock }) },
  ),
}));

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

vi.mock('@/lib/api', () => ({
  fetchPulseToday: vi.fn(),
  ratePulseCard: vi.fn(),
  fetchMyDay: vi.fn(),
  getStats: vi.fn(),
}));

const { fetchPulseToday } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HeroNow />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('HeroNow — localStorage persistence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
  });

  it('hydrates to "Continue task" tab when localStorage pre-set to "task"', async () => {
    // Pre-set BEFORE render
    localStorage.setItem('myday.heroMode', 'task');

    renderWithProviders();

    // HeroNow reads localStorage in useEffect; after hydration the "task" tab should be active
    const taskTab = await screen.findByRole('tab', { name: 'Continue task' });
    expect(taskTab).toHaveAttribute('aria-selected', 'true');

    // Pulse tab should NOT be selected
    const pulseTab = screen.getByRole('tab', { name: 'Pulse #1' });
    expect(pulseTab).toHaveAttribute('aria-selected', 'false');
  });

  it('switching from pulse to task updates localStorage to "task"', async () => {
    const user = userEvent.setup();
    // Pre-set to 'pulse' explicitly
    localStorage.setItem('myday.heroMode', 'pulse');

    renderWithProviders();

    // Wait for render
    await screen.findByText(/No Pulse for today yet/i);

    // Verify pulse tab is initially active
    expect(screen.getByRole('tab', { name: 'Pulse #1' })).toHaveAttribute('aria-selected', 'true');

    // Switch to task tab
    const taskTab = screen.getByRole('tab', { name: 'Continue task' });
    await user.click(taskTab);

    // localStorage should have been updated
    expect(localStorage.getItem('myday.heroMode')).toBe('task');
  });
});

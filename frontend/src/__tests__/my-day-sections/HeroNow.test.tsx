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

// Import after mock to get the mocked version
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

describe('HeroNow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Default: fetchPulseToday returns null (no deck yet)
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
  });

  it('defaults to Pulse mode on first render with empty localStorage', async () => {
    renderWithProviders();

    // Wait for the component to stabilize (HeroPulse renders)
    expect(await screen.findByText(/No Pulse for today yet/i)).toBeInTheDocument();

    // The "Pulse #1" tab trigger should exist (it is the active tab)
    const pulseTab = screen.getByRole('tab', { name: 'Pulse #1' });
    expect(pulseTab).toBeInTheDocument();
    // Radix Tabs sets aria-selected="true" on the active trigger
    expect(pulseTab).toHaveAttribute('aria-selected', 'true');
  });

  it('clicking "Continue task" tab switches to Task mode and persists to localStorage', async () => {
    const user = userEvent.setup();
    renderWithProviders();

    // Wait for initial render
    await screen.findByText(/No Pulse for today yet/i);

    const taskTab = screen.getByRole('tab', { name: 'Continue task' });
    await user.click(taskTab);

    // localStorage should now contain 'task'
    expect(localStorage.getItem('myday.heroMode')).toBe('task');

    // The task tab should now be selected
    expect(taskTab).toHaveAttribute('aria-selected', 'true');
  });

  it('"Resume reading" tab trigger is disabled', async () => {
    renderWithProviders();

    // Wait for component to render
    await screen.findByText(/No Pulse for today yet/i);

    // The Resume reading trigger is wrapped in a span for Tooltip compatibility.
    // The actual TabsTrigger element has disabled attribute.
    const resumeTab = screen.getByRole('tab', { name: 'Resume reading' });
    expect(resumeTab).toBeDisabled();
  });
});

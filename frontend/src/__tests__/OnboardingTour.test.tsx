import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { OnboardingTour } from '@/components/onboarding/OnboardingTour';
import * as api from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ── Mocks ─────────────────────────────────────────────────────────────────

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchTopics: vi.fn(),
    fetchFeed: vi.fn(),
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: 'onboarding.dismissed', value: true }),
  };
});

// react-joyride renders a portal and uses window.requestAnimationFrame — mock
// it minimally so jsdom doesn't explode on the missing DOM APIs.
vi.mock('react-joyride', () => {
  const MockJoyride = ({
    run,
    steps,
    stepIndex = 0,
    locale,
    onEvent,
  }: {
    run: boolean;
    steps: Array<{ target: string; title?: string; content: string }>;
    stepIndex?: number;
    locale?: { skip?: string };
    onEvent?: (
      data: { action: string; index: number; status: string; type: string },
      controls: { info: () => { size: number } },
    ) => void;
  }) => {
    if (!run || steps.length === 0) return null;
    const currentStep = steps[stepIndex]!;
    const controls = { info: () => ({ size: steps.length }) };
    const emit = (data: { action: string; index: number; status: string; type: string }) =>
      onEvent?.(data, controls);
    return (
      <div data-testid="joyride-tour" role="dialog" aria-label="Research workflow tour">
        <div data-testid="joyride-step-title">{currentStep.title}</div>
        <div data-testid="joyride-step-content">{currentStep.content}</div>
        <ol data-testid="joyride-step-contract">
          {steps.map((step) => (
            <li key={String(step.title)}>{`${String(step.title)}|${step.target}`}</li>
          ))}
        </ol>
        <button
          data-testid="joyride-back"
          disabled={stepIndex === 0}
          onClick={() =>
            emit({
              action: 'prev',
              index: stepIndex,
              status: 'running',
              type: 'step:after',
            })
          }
        >
          Back
        </button>
        <button
          ref={(button) => button?.focus()}
          data-testid="joyride-next"
          onClick={() =>
            emit(
              stepIndex === steps.length - 1
                ? {
                    action: 'next',
                    index: stepIndex,
                    status: 'finished',
                    type: 'tour:end',
                  }
                : {
                    action: 'next',
                    index: stepIndex,
                    status: 'running',
                    type: 'step:after',
                  },
            )
          }
        >
          {stepIndex === steps.length - 1 ? 'Done' : 'Next'}
        </button>
        <button
          data-testid="joyride-skip"
          onClick={() =>
            emit({
              action: 'skip',
              index: 0,
              status: 'skipped',
              type: 'tour:end',
            })
          }
        >
          {locale?.skip ?? "Don't show again"}
        </button>
        <button
          data-testid="joyride-target-not-found"
          onClick={() =>
            emit({
              action: 'next',
              index: stepIndex,
              status: 'running',
              type: 'error:target_not_found',
            })
          }
        >
          Simulate missing target
        </button>
      </div>
    );
  };
  return {
    Joyride: MockJoyride,
    ACTIONS: { PREV: 'prev' },
    EVENTS: { STEP_AFTER: 'step:after', TARGET_NOT_FOUND: 'error:target_not_found' },
    STATUS: { FINISHED: 'finished', SKIPPED: 'skipped' },
  };
});

// ── Test helpers ───────────────────────────────────────────────────────────

function renderTour() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <OnboardingTour />
    </MemoryRouter>,
    { queryClient },
  );
}

// ── Tests ──────────────────────────────────────────────────────────────────

describe('OnboardingTour', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Clear the localStorage dismissed flag so each test starts clean.
    localStorage.removeItem('jarvis-onboarding-dismissed');
    // Default: admin user.
    useAuthStore.setState({ user: { id: 1, email: 'a@b.com', role: 'admin' } });

    // Default: new user — zero papers.
    vi.mocked(api.fetchFeed).mockResolvedValue({ papers: [], total: 0 });
    vi.mocked(api.fetchConfig).mockResolvedValue([]);
  });

  it('renders the tour for a new user with no papers', async () => {
    renderTour();

    // The tour should appear once the eligibility queries resolve.
    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });

    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Discover Papers');
    expect(screen.getByTestId('joyride-step-contract')).toHaveTextContent(
      'Discover Papers|[data-tour-id="sidebar-discover"]',
    );
    expect(screen.getByTestId('joyride-step-contract')).toHaveTextContent(
      'Save to Your Library|[data-tour-id~="sidebar-library"]',
    );
    expect(screen.getByTestId('joyride-step-contract')).toHaveTextContent(
      'Analyze a Paper|[data-tour-id~="sidebar-analyze"]',
    );
    expect(screen.getByTestId('joyride-step-contract')).toHaveTextContent(
      'Ask Across Your Library|[data-tour-id="sidebar-ask"]',
    );
  });

  it('does NOT render the tour when onboarding_dismissed is persisted in localStorage', async () => {
    // Pre-set the dismissed flag.
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');

    renderTour();

    // Give the eligibility queries time to resolve — tour should still be absent.
    await waitFor(() => {
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled();
    });

    expect(screen.queryByTestId('joyride-tour')).not.toBeInTheDocument();
  });

  it('does NOT render the tour when dismissal is persisted for the user on the server', async () => {
    vi.mocked(api.fetchConfig).mockResolvedValue([
      { key: 'onboarding.dismissed', value: true },
    ]);

    renderTour();

    await waitFor(() => {
      expect(vi.mocked(api.fetchConfig)).toHaveBeenCalled();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 900));
    });
    expect(screen.queryByTestId('joyride-tour')).not.toBeInTheDocument();
  });

  it('renders the tour after setup creates a topic but before any papers exist', async () => {
    vi.mocked(api.fetchTopics).mockResolvedValue([
      {
        id: 1,
        name: 'Neural ODEs',
        description: null,
        query_terms: [],
        category: null,
        enabled: true,
        created_at: new Date().toISOString(),
      },
    ]);

    renderTour();

    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });
    expect(vi.mocked(api.fetchTopics)).not.toHaveBeenCalled();
  });

  it('does not infer first-use eligibility when the feed check fails', async () => {
    vi.mocked(api.fetchFeed).mockRejectedValue(new Error('feed unavailable'));

    renderTour();

    await waitFor(() => {
      expect(vi.mocked(api.fetchFeed)).toHaveBeenCalled();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 900));
    });
    expect(screen.queryByTestId('joyride-tour')).not.toBeInTheDocument();
  });

  it('persists onboarding_dismissed=true when the skip button is clicked', async () => {
    const user = userEvent.setup({ delay: null });
    renderTour();

    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });

    await user.click(screen.getByTestId('joyride-skip'));

    // setConfig must be called with the dismissal key.
    await waitFor(() => {
      expect(vi.mocked(api.setConfig)).toHaveBeenCalledWith('onboarding.dismissed', true);
    });

    // localStorage flag must also be set.
    expect(localStorage.getItem('jarvis-onboarding-dismissed')).toBe('true');
  });

  it('supports focused Next, Back, and finish without losing the step order', async () => {
    const user = userEvent.setup({ delay: null });
    renderTour();

    const next = await screen.findByTestId('joyride-next', {}, { timeout: 2000 });
    expect(next).toHaveFocus();
    await user.click(next);
    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Save to Your Library');

    await user.click(screen.getByTestId('joyride-back'));
    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Discover Papers');

    await user.click(screen.getByTestId('joyride-next'));
    await user.click(screen.getByTestId('joyride-next'));
    await user.click(screen.getByTestId('joyride-next'));
    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Ask Across Your Library');
    await user.click(screen.getByTestId('joyride-next'));

    await waitFor(() => {
      expect(vi.mocked(api.setConfig)).toHaveBeenCalledWith('onboarding.dismissed', true);
    });
    expect(localStorage.getItem('jarvis-onboarding-dismissed')).toBe('true');
  });

  it('advances when the current target is unavailable', async () => {
    const user = userEvent.setup({ delay: null });
    renderTour();

    await screen.findByTestId('joyride-next', {}, { timeout: 2000 });
    await user.click(screen.getByTestId('joyride-target-not-found'));

    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Save to Your Library');
  });

  it('uses always-visible body targets on narrow layouts', async () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      media: '(max-width: 767px)',
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    });

    try {
      renderTour();
      const contract = await screen.findByTestId('joyride-step-contract', {}, { timeout: 2000 });
      for (const item of contract.querySelectorAll('li')) {
        expect(item).toHaveTextContent('|body');
      }
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it('starts with Discover for admin users', async () => {
    useAuthStore.setState({ user: { id: 1, email: 'admin@example.com', role: 'admin' } });
    renderTour();

    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });

    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Discover Papers');
    expect(screen.getByTestId('joyride-step-content')).toHaveTextContent('Discover');
  });

  it('uses the same researcher workflow for member users', async () => {
    useAuthStore.setState({ user: { id: 2, email: 'member@example.com', role: 'user' } });
    renderTour();

    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });

    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Discover Papers');
    expect(screen.getAllByTestId('joyride-step-contract')).toHaveLength(1);
    expect(screen.getByTestId('joyride-step-contract').querySelectorAll('li')).toHaveLength(4);
  });
});

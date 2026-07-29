import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
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
    setConfig: vi.fn().mockResolvedValue({ key: 'onboarding.dismissed', value: true }),
  };
});

// react-joyride renders a portal and uses window.requestAnimationFrame — mock
// it minimally so jsdom doesn't explode on the missing DOM APIs.
vi.mock('react-joyride', () => {
  const MockJoyride = ({
    run,
    steps,
    locale,
    callback,
  }: {
    run: boolean;
    steps: Array<{ title?: string; content: string }>;
    locale?: { skip?: string };
    callback?: (data: { action: string; index: number; status: string; type: string }) => void;
  }) => {
    if (!run || steps.length === 0) return null;
    const firstStep = steps[0]!;
    return (
      <div data-testid="joyride-tour">
        <div data-testid="joyride-step-title">{firstStep.title}</div>
        <div data-testid="joyride-step-content">{firstStep.content}</div>
        <button
          data-testid="joyride-skip"
          onClick={() =>
            callback?.({
              action: 'skip',
              index: 0,
              status: 'skipped',
              type: 'tour:end',
            })
          }
        >
          {locale?.skip ?? "Don't show again"}
        </button>
      </div>
    );
  };
  return { default: MockJoyride, ACTIONS: { PREV: 'prev' }, EVENTS: { STEP_AFTER: 'step:after', TARGET_NOT_FOUND: 'error:target_not_found' }, STATUS: { FINISHED: 'finished', SKIPPED: 'skipped' } };
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

    // Default: new user — zero topics, zero papers.
    vi.mocked(api.fetchTopics).mockResolvedValue([]);
    vi.mocked(api.fetchFeed).mockResolvedValue({ papers: [], total: 0 });
  });

  it('renders the tour for a new user with no topics and no papers', async () => {
    renderTour();

    // The tour should appear once the eligibility queries resolve.
    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });

    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Connect a Source');
  });

  it('does NOT render the tour when onboarding_dismissed is persisted in localStorage', async () => {
    // Pre-set the dismissed flag.
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');

    renderTour();

    // Give the eligibility queries time to resolve — tour should still be absent.
    await waitFor(() => {
      expect(vi.mocked(api.fetchTopics)).toHaveBeenCalled();
    });

    expect(screen.queryByTestId('joyride-tour')).not.toBeInTheDocument();
  });

  it('does NOT render the tour when the user already has topics', async () => {
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
    ] as import('@/types').Topic[]);

    renderTour();

    await waitFor(() => {
      expect(vi.mocked(api.fetchTopics)).toHaveBeenCalled();
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

  it('shows Sources as first step for admin users', async () => {
    useAuthStore.setState({ user: { id: 1, email: 'admin@example.com', role: 'admin' } });
    renderTour();

    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });

    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Connect a Source');
    expect(screen.getByTestId('joyride-step-content')).toHaveTextContent('left rail');
  });

  it('starts at Topics (not Sources) for member users', async () => {
    useAuthStore.setState({ user: { id: 2, email: 'member@example.com', role: 'user' } });
    renderTour();

    await waitFor(() => {
      expect(screen.getByTestId('joyride-tour')).toBeInTheDocument();
    }, { timeout: 2000 });

    expect(screen.getByTestId('joyride-step-title')).toHaveTextContent('Define a Topic');
    expect(screen.queryByTestId('joyride-step-title')).not.toHaveTextContent('Connect a Source');
  });
});

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { useCommandPalette } from '@/stores/command-palette-store';
import { useNavPrefsStore } from '@/stores/nav-prefs-store';
import { useResearchMilestoneStore } from '@/stores/research-milestone-store';

vi.mock('@/hooks/use-pomodoro-tick', () => ({ usePomodoroTick: vi.fn() }));
vi.mock('@/hooks/use-theme-effect', () => ({ useThemeEffect: vi.fn() }));
vi.mock('@/hooks/use-appearance', () => ({ useAppearance: vi.fn() }));
vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (state: { hydrate: () => void }) => unknown) =>
    selector({ hydrate: vi.fn() }),
  registerVisibilityHydrate: () => () => {},
}));
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: () => ({
    user: { id: 1, email: 'reader@example.com', role: 'user' },
    logout: vi.fn(),
  }),
}));
vi.mock('@/stores/ui-store', () => ({
  useUIStore: () => ({ sidebarCollapsed: false, toggleSidebar: vi.fn() }),
}));
vi.mock('@/components/shared/HealthDots', () => ({ HealthDots: () => null }));
vi.mock('@/components/layout/TopBar', () => ({
  TopBar: ({ onMenuClick }: { onMenuClick?: () => void }) => (
    <button type="button" onClick={onMenuClick}>Open menu</button>
  ),
}));
vi.mock('@/components/layout/ConnectivityBanner', () => ({ ConnectivityBanner: () => null }));
vi.mock('@/components/shared/MaintenanceBanner', () => ({ MaintenanceBanner: () => null }));
vi.mock('@/components/ui/toaster', () => ({ Toaster: () => null }));
vi.mock('@/components/shared/KeyboardCheatSheet', () => ({ KeyboardCheatSheet: () => null }));
vi.mock('@/components/onboarding/OnboardingTour', () => ({ default: () => null }));

function renderShell() {
  return render(
    <MemoryRouter>
      <AppShell><section>Page content</section></AppShell>
    </MemoryRouter>,
  );
}

describe('AppShell accessibility', () => {
  beforeEach(() => {
    useCommandPalette.getState()._reset();
    useNavPrefsStore.setState({ navMode: 'full' });
    useResearchMilestoneStore.setState({
      completed: { save: false, analyze: false },
      advancedCueDismissed: false,
    });
  });

  it('places a focus-revealed skip link before every other tabbable element', async () => {
    const user = userEvent.setup();
    const { container } = renderShell();
    const skipLink = screen.getByRole('link', { name: 'Skip to main content' });
    const firstTabbable = container.querySelector('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])');

    expect(firstTabbable).toBe(skipLink);
    expect(skipLink).toHaveAttribute('href', '#main-content');
    expect(skipLink).toHaveClass('sr-only', 'focus:not-sr-only');
    await user.tab();
    expect(skipLink).toHaveFocus();
    expect(container.querySelectorAll('main')).toHaveLength(1);
    expect(container.querySelector('main')).toHaveAttribute('id', 'main-content');
  });

  it('offers the shared paper search in the mobile drawer without a collapse control', async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByRole('button', { name: 'Open menu' }));

    const drawer = await screen.findByRole('dialog', { name: 'Navigation' });
    const search = within(drawer).getByRole('button', { name: 'Search your papers…' });
    expect(within(drawer).queryByRole('button', { name: /collapse sidebar/i })).toBeNull();
    expect(within(drawer).queryByRole('button', { name: /expand sidebar/i })).toBeNull();

    await user.click(search);
    expect(useCommandPalette.getState().isOpen).toBe(true);
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull());
  });
});

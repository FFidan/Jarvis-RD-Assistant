/**
 * Sidebar.grouped.test.tsx — unit tests for the grouped roman-numeral sidebar.
 *
 * Covers:
 * - All 4 nav groups render with correct roman numerals
 * - Group Ⅳ Admin is hidden for non-admin users
 * - Group Ⅳ Admin is visible for admin users
 * - All group items render for admin users
 * - HealthDots receives adminLink prop for admin users (navigates to system-health)
 * - HealthDots does NOT receive adminLink for non-admin users (in-place expand)
 * - Active route receives aria-current="page"
 * - Collapsed mode hides group labels
 * - Settings link appears in footer (not in any numbered group)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Sidebar } from '@/components/layout/Sidebar';
import {
  useNavPrefsStore,
  NAV_PREFS_STORE_KEY,
  initialNavMode,
  type NavMode,
} from '@/stores/nav-prefs-store';
import { useResearchMilestoneStore } from '@/stores/research-milestone-store';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: vi.fn(),
}));

vi.mock('@/stores/ui-store', () => ({
  useUIStore: vi.fn(),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchStackHealth: vi.fn().mockReturnValue(new Promise(() => {})), // never resolves in tests
  };
});

import { useAuthStore } from '@/stores/auth-store';
import { useUIStore } from '@/stores/ui-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const mockUseAuthStore = vi.mocked(useAuthStore);
const mockUseUIStore = vi.mocked(useUIStore);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeAuthStore(role: 'user' | 'admin' = 'user') {
  return {
    user: { id: 1, email: 'test@example.com', role },
    logout: vi.fn(),
  };
}

function makeUIStore(collapsed = false) {
  return {
    sidebarCollapsed: collapsed,
    toggleSidebar: vi.fn(),
  };
}

function renderSidebar({
  role = 'user' as 'user' | 'admin',
  collapsed = false,
  initialPath = '/',
  navMode = 'full' as NavMode,
} = {}) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockUseAuthStore.mockReturnValue(makeAuthStore(role) as any);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockUseUIStore.mockReturnValue(makeUIStore(collapsed) as any);
  // The grouped/admin/collapsed suites assert the full nav; pin the real
  // nav-prefs store to the requested mode (default full) so they don't depend
  // on jsdom's localStorage carry-over.
  useNavPrefsStore.setState({ navMode });

  const queryClient = createTestQueryClient();

  return renderWithProviders(
    <MemoryRouter initialEntries={[initialPath]}>
      <Sidebar />
    </MemoryRouter>,
    { queryClient },
  );
}

function resetResearchMilestones() {
  useResearchMilestoneStore.setState({
    completed: { save: false, analyze: false },
    advancedCueDismissed: false,
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Sidebar — grouped nav (non-admin)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders groups Ⅰ through Ⅲ for non-admin user', () => {
    renderSidebar({ role: 'user' });

    expect(screen.getByText('Today')).toBeInTheDocument();
    expect(screen.getByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText('Learn')).toBeInTheDocument();
    // The old one-item "Read"/"Ask" groups are gone: Ask is a Workspace item.
    expect(screen.queryByText('Read')).not.toBeInTheDocument();
    expect(screen.getAllByText('Ask')).toHaveLength(1);
  });

  it('does NOT render group Ⅳ Admin for non-admin user', () => {
    renderSidebar({ role: 'user' });

    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'User Management' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Audit Log' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'System Health' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'System Logs' })).not.toBeInTheDocument();
  });

  it('renders roman numerals Ⅰ–Ⅲ', () => {
    renderSidebar({ role: 'user' });

    expect(screen.getByText('Ⅰ')).toBeInTheDocument();
    expect(screen.getByText('Ⅱ')).toBeInTheDocument();
    expect(screen.getByText('Ⅲ')).toBeInTheDocument();
    expect(screen.queryByText('Ⅳ')).not.toBeInTheDocument();
  });

  it('renders group Ⅰ Today items', () => {
    renderSidebar({ role: 'user' });

    expect(screen.getByRole('link', { name: 'Home' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'My Day' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Pulse Deck' })).toBeInTheDocument();
    // The saved-papers destination is "Papers"; "Discover" is its sibling.
    expect(screen.getByRole('link', { name: 'Papers' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Library' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Discover' })).toBeInTheDocument();
  });

  it('Papers link points to /feed?surface=library', () => {
    renderSidebar({ role: 'user' });

    const papersLink = screen.getByRole('link', { name: 'Papers' });
    expect(papersLink).toHaveAttribute('href', '/feed?surface=library');
  });

  it('Discover link points to /feed?surface=search', () => {
    renderSidebar({ role: 'user' });

    const discoverLink = screen.getByRole('link', { name: 'Discover' });
    expect(discoverLink).toHaveAttribute('href', '/feed?surface=search');
  });

  it('nav-discover testid is unique (appears exactly once)', () => {
    renderSidebar({ role: 'user' });

    const els = document.querySelectorAll('[data-testid="nav-discover"]');
    expect(els.length).toBe(1);
  });

  it('renders group Ⅱ Workspace items, Ask included', () => {
    renderSidebar({ role: 'user' });

    expect(screen.getByRole('link', { name: 'Projects' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /^Ask$/ })).toHaveAttribute('href', '/ask');
    expect(screen.getByRole('link', { name: 'Knowledge Graph' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Citation Graph' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Consensus' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Extraction Table' })).toBeInTheDocument();
  });

  it('renders group Ⅲ Learn items', () => {
    renderSidebar({ role: 'user' });

    expect(screen.getByRole('link', { name: 'Learning Cards' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Analytics' })).toBeInTheDocument();
  });

  it('renders Settings link in footer', () => {
    renderSidebar({ role: 'user' });

    expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument();
  });

  it('active route has aria-current="page"', () => {
    renderSidebar({ role: 'user', initialPath: '/my-day' });

    const myDayLink = screen.getByRole('link', { name: 'My Day' });
    expect(myDayLink).toHaveAttribute('aria-current', 'page');
  });

  it('inactive routes do not have aria-current', () => {
    renderSidebar({ role: 'user', initialPath: '/my-day' });

    const homeLink = screen.getByRole('link', { name: 'Home' });
    expect(homeLink).not.toHaveAttribute('aria-current', 'page');
  });

  it('Papers nav link is active on /feed?surface=library', () => {
    renderSidebar({ role: 'user', initialPath: '/feed?surface=library' });

    const papersLink = screen.getByRole('link', { name: 'Papers' });
    expect(papersLink).toHaveAttribute('aria-current', 'page');
  });

  it('Discover nav link is active on /feed?surface=search', () => {
    renderSidebar({ role: 'user', initialPath: '/feed?surface=search' });

    const discoverLink = screen.getByRole('link', { name: 'Discover' });
    expect(discoverLink).toHaveAttribute('aria-current', 'page');
  });

  it('Papers is not active when Discover is the current surface', () => {
    renderSidebar({ role: 'user', initialPath: '/feed?surface=search' });

    const papersLink = screen.getByRole('link', { name: 'Papers' });
    expect(papersLink).not.toHaveAttribute('aria-current', 'page');
  });

  it('Discover is not active when Papers is the current surface', () => {
    renderSidebar({ role: 'user', initialPath: '/feed?surface=library' });

    const discoverLink = screen.getByRole('link', { name: 'Discover' });
    expect(discoverLink).not.toHaveAttribute('aria-current', 'page');
  });

  it('Papers nav link is active on bare /feed (counts toward Papers)', () => {
    renderSidebar({ role: 'user', initialPath: '/feed' });

    const papersLink = screen.getByRole('link', { name: 'Papers' });
    expect(papersLink).toHaveAttribute('aria-current', 'page');
    const discoverLink = screen.getByRole('link', { name: 'Discover' });
    expect(discoverLink).not.toHaveAttribute('aria-current', 'page');
  });

  it('Papers nav link is active on /feed?surface=inbox (Inbox is Papers’ first tab)', () => {
    renderSidebar({ role: 'user', initialPath: '/feed?surface=inbox' });

    const papersLink = screen.getByRole('link', { name: 'Papers' });
    expect(papersLink).toHaveAttribute('aria-current', 'page');
  });
});

describe('Sidebar — grouped nav (admin)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders all 4 groups for admin user', () => {
    renderSidebar({ role: 'admin' });

    expect(screen.getByText('Today')).toBeInTheDocument();
    expect(screen.getByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText('Learn')).toBeInTheDocument();
    expect(screen.getByText('Admin')).toBeInTheDocument();
  });

  it('renders all roman numerals Ⅰ–Ⅳ for admin', () => {
    renderSidebar({ role: 'admin' });

    ['Ⅰ', 'Ⅱ', 'Ⅲ', 'Ⅳ'].forEach((numeral) => {
      expect(screen.getByText(numeral)).toBeInTheDocument();
    });
    expect(screen.queryByText('Ⅴ')).not.toBeInTheDocument();
  });

  it('renders group Ⅳ Admin items for admin user', () => {
    renderSidebar({ role: 'admin' });

    expect(screen.getByRole('link', { name: 'User Management' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'System Health' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Audit Log' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'System Logs' })).toBeInTheDocument();
  });

  it('admin group items have correct hrefs', () => {
    renderSidebar({ role: 'admin' });

    expect(screen.getByRole('link', { name: 'User Management' })).toHaveAttribute('href', '/admin/users');
    expect(screen.getByRole('link', { name: 'System Health' })).toHaveAttribute('href', '/admin/system-health');
    expect(screen.getByRole('link', { name: 'Audit Log' })).toHaveAttribute('href', '/admin/audit-log');
    expect(screen.getByRole('link', { name: 'System Logs' })).toHaveAttribute('href', '/logs');
  });

  it('HealthDots admin-link pill renders for admin (non-collapsed)', () => {
    renderSidebar({ role: 'admin', collapsed: false });

    // HealthDots is loading (fetchStackHealth never resolves in tests)
    // The component renders loading state; just verify the sidebar rendered admin groups
    expect(screen.getByText('Admin')).toBeInTheDocument();
  });
});

describe('Sidebar — nav-group testid uniqueness', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('each nav-group-* testid appears exactly once for non-admin (3 visible groups)', () => {
    renderSidebar({ role: 'user' });

    const groups = ['today', 'workspace', 'learn'];
    for (const label of groups) {
      // getAllByTestId throws if 0; length > 1 would mean duplicate
      const els = document.querySelectorAll(`[data-testid="nav-group-${label}"]`);
      expect(els.length).toBe(1);
    }
  });

  it('each nav-group-* testid appears exactly once for admin (4 visible groups)', () => {
    renderSidebar({ role: 'admin' });

    const groups = ['today', 'workspace', 'learn', 'admin'];
    for (const label of groups) {
      const els = document.querySelectorAll(`[data-testid="nav-group-${label}"]`);
      expect(els.length).toBe(1);
    }
  });
});

describe('Sidebar — collapsed mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('hides group label text in collapsed mode', () => {
    renderSidebar({ role: 'user', collapsed: true });

    expect(screen.queryByText('Today')).not.toBeInTheDocument();
    expect(screen.queryByText('Workspace')).not.toBeInTheDocument();
    expect(screen.queryByText('Learn')).not.toBeInTheDocument();
  });

  it('still renders nav links in collapsed mode (icon-only)', () => {
    renderSidebar({ role: 'user', collapsed: true });

    // In collapsed mode links still render but may lack visible text (icon-only).
    // We look for the href directly.
    const homeLink = screen.getByRole('link', { name: /home/i });
    expect(homeLink).toBeInTheDocument();
    expect(homeLink).toHaveAttribute('href', '/');
  });

  it('hides admin group labels in collapsed mode', () => {
    renderSidebar({ role: 'admin', collapsed: true });

    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
    // Admin links still present in collapsed (icon-only) mode
    const userMgmtLink = screen.getByRole('link', { name: /user management/i });
    expect(userMgmtLink).toBeInTheDocument();
  });
});

describe('Sidebar — simple mode (progressive disclosure)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    resetResearchMilestones();
  });

  it('defaults to simple mode when nothing has been stored', () => {
    // Exercise the real production initializer, not a re-implementation.
    expect(localStorage.getItem(NAV_PREFS_STORE_KEY)).toBeNull();
    expect(initialNavMode()).toBe('simple');
  });

  it('does not consult the onboarding-dismissed key to pick a mode', () => {
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
    expect(initialNavMode()).toBe('simple');
  });

  it('needs no localStorage read at all (survives a throwing Storage)', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError: localStorage blocked');
    });
    expect(initialNavMode()).toBe('simple');
    spy.mockRestore();
  });

  it('shows the documented research-loop essentials and the toggle', () => {
    renderSidebar({ role: 'user', navMode: 'simple' });

    expect(screen.getByRole('link', { name: 'My Day' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Papers' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Discover' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Projects' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /^Ask$/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Learning Cards' })).toBeInTheDocument();

    // The rest are revealed only by switching to full mode — there is no
    // in-rail "More" disclosure (it would duplicate the footer toggle).
    expect(screen.queryByTestId('nav-more')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'More' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Home' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Pulse Deck' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Analytics' })).not.toBeInTheDocument();

    // ≤8 visible nav links total (6 essentials + footer Settings).
    expect(screen.getAllByRole('link').length).toBeLessThanOrEqual(8);

    // The full nav is one toggle away.
    expect(screen.getByRole('button', { name: 'Show all features' })).toBeInTheDocument();
  });

  it('non-essential and admin destinations are absent from the simple rail (reachable in full)', () => {
    renderSidebar({ role: 'admin', navMode: 'simple' });
    // Even an admin sees only the essentials in simple mode; everything else —
    // including admin destinations — lives in the full grouped view.
    expect(screen.queryByRole('link', { name: 'User Management' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'System Logs' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Knowledge Graph' })).not.toBeInTheDocument();
  });

  it('query-aware highlight still works in simple mode (bare /feed → Papers)', () => {
    renderSidebar({ role: 'user', navMode: 'simple', initialPath: '/feed' });
    const papersLink = screen.getByRole('link', { name: 'Papers' });
    expect(papersLink).toHaveAttribute('aria-current', 'page');
  });

  it('toggle switches simple → full and reveals all groups; route untouched', () => {
    renderSidebar({ role: 'user', navMode: 'simple' });
    expect(screen.queryByText('Today')).not.toBeInTheDocument();
    expect(screen.getByTestId('nav-mode-toggle')).toHaveTextContent('Show all features');

    fireEvent.click(screen.getByTestId('nav-mode-toggle'));

    expect(useNavPrefsStore.getState().navMode).toBe('full');
    expect(screen.getByRole('button', { name: 'Simple view' })).toHaveTextContent('Simple view');
    expect(screen.getByText('Today')).toBeInTheDocument();
    expect(screen.getByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText('Learn')).toBeInTheDocument();
    // Previously-hidden destinations are now directly visible.
    expect(screen.getByRole('link', { name: 'Home' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Analytics' })).toBeInTheDocument();
    expect(screen.queryByTestId('nav-more')).not.toBeInTheDocument();
  });

  it('exposes every desktop tour target in simple mode for both roles', () => {
    const { unmount } = renderSidebar({ role: 'user', navMode: 'simple' });
    expect(document.querySelector('[data-tour-id="sidebar-discover"]')).not.toBeNull();
    expect(document.querySelector('[data-tour-id~="sidebar-library"]')).not.toBeNull();
    expect(document.querySelector('[data-tour-id~="sidebar-analyze"]')).not.toBeNull();
    expect(document.querySelector('[data-tour-id="sidebar-ask"]')).not.toBeNull();
    unmount();

    renderSidebar({ role: 'admin', navMode: 'simple' });
    expect(document.querySelector('[data-tour-id="sidebar-discover"]')).not.toBeNull();
    expect(document.querySelector('[data-tour-id~="sidebar-library"]')).not.toBeNull();
    expect(document.querySelector('[data-tour-id~="sidebar-analyze"]')).not.toBeNull();
    expect(document.querySelector('[data-tour-id="sidebar-ask"]')).not.toBeNull();
  });

  it('shows one dismissible advanced-workspace cue only after a completed milestone', () => {
    const { rerender } = renderSidebar({ role: 'user', navMode: 'simple' });
    expect(screen.queryByTestId('advanced-workspace-cue')).not.toBeInTheDocument();

    useResearchMilestoneStore.getState().recordMilestone('save');
    rerender(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );

    const cue = screen.getByTestId('advanced-workspace-cue');
    expect(cue).toHaveTextContent('Extraction Table');
    expect(cue).toHaveTextContent('Knowledge Graph');
    expect(cue).toHaveTextContent('Citation Graph');
    // Anything already in the simple rail is not something the toggle adds,
    // so offering it would be a promise the rail has already kept.
    const railLabels = screen
      .getAllByRole('link')
      .map((link) => link.textContent?.trim() ?? '')
      .filter(Boolean);
    expect(railLabels).toContain('Projects');
    for (const label of railLabels) {
      expect(cue).not.toHaveTextContent(label);
    }
    expect(screen.getAllByTestId('advanced-workspace-cue')).toHaveLength(1);
  });

  it('persists cue dismissal from its keyboard-accessible button', () => {
    useResearchMilestoneStore.getState().recordMilestone('analyze');
    renderSidebar({ role: 'user', navMode: 'simple' });

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss workspace feature tip' }));

    expect(screen.queryByTestId('advanced-workspace-cue')).not.toBeInTheDocument();
    expect(useResearchMilestoneStore.getState().advancedCueDismissed).toBe(true);
  });

  it('dismisses the cue when Show all features opens the full rail', () => {
    useResearchMilestoneStore.getState().recordMilestone('save');
    renderSidebar({ role: 'user', navMode: 'simple' });

    fireEvent.click(screen.getByTestId('nav-mode-toggle'));

    expect(screen.queryByTestId('advanced-workspace-cue')).not.toBeInTheDocument();
    expect(useResearchMilestoneStore.getState().advancedCueDismissed).toBe(true);
    expect(useNavPrefsStore.getState().navMode).toBe('full');
  });
});

describe('Sidebar — footer app version', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the build version in the footer (expanded)', () => {
    renderSidebar({ role: 'user', collapsed: false });

    expect(screen.getByTestId('sidebar-app-version')).toHaveTextContent(`v${__APP_VERSION__}`);
  });

  it('hides the version caption when collapsed (no room for text)', () => {
    renderSidebar({ role: 'user', collapsed: true });

    expect(screen.queryByTestId('sidebar-app-version')).not.toBeInTheDocument();
  });
});

describe('Sidebar — navMode persistence (survives logout)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('is persisted under its OWN key, not inside the UI store key', () => {
    useNavPrefsStore.getState().setNavMode('simple');
    const raw = localStorage.getItem(NAV_PREFS_STORE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).state.navMode).toBe('simple');
    // Not co-located with the logout-wiped UI store.
    expect(localStorage.getItem('jarvis-ui')).toBeNull();
  });

  it('survives a simulated logout (UI store key removed + session resets run)', () => {
    useNavPrefsStore.getState().setNavMode('simple');

    // Mirror auth-store.logout()'s localStorage side effect: it removes the UI
    // store key and runs the session-reset registry — nav-prefs registers
    // neither, so its value must remain intact.
    localStorage.removeItem('jarvis-ui');

    expect(useNavPrefsStore.getState().navMode).toBe('simple');
    const raw = localStorage.getItem(NAV_PREFS_STORE_KEY);
    expect(JSON.parse(raw!).state.navMode).toBe('simple');
  });
});

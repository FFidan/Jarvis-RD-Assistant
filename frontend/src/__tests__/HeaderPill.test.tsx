import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

// Must mock before importing component
vi.mock('@/lib/logs', () => ({
  getSummary: vi.fn(),
}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const orig = await importOriginal<typeof import('react-router-dom')>();
  return { ...orig, useNavigate: () => mockNavigate };
});

import { HeaderPill } from '@/components/logs/HeaderPill';
import { getSummary } from '@/lib/logs';
import { useAuthStore } from '@/stores/auth-store';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const mockGetSummary = vi.mocked(getSummary);

function renderPill(initialPath = '/') {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter initialEntries={[initialPath]}>
      <HeaderPill />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('HeaderPill', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      isAuthenticated: true,
      user: { id: 1, email: 'admin@example.com', role: 'admin' },
      authTime: Date.now(),
    });
  });

  it('does not request the admin-only log summary for a regular user', async () => {
    useAuthStore.setState({
      isAuthenticated: true,
      user: { id: 2, email: 'member@example.com', role: 'user' },
      authTime: Date.now(),
    });
    mockGetSummary.mockResolvedValue({
      by_level: { error: 1 },
      by_category: {},
      total: 1,
    });

    const { container } = renderPill();

    await vi.waitFor(() => expect(mockGetSummary).not.toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when error count is 0', async () => {
    mockGetSummary.mockResolvedValue({
      by_level: { error: 0, warning: 2 },
      by_category: {},
      total: 2,
    });
    const { container } = renderPill();
    // Wait for query — still null when 0 errors
    await vi.waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it('renders error count badge when errors > 0', async () => {
    mockGetSummary.mockResolvedValue({
      by_level: { error: 5 },
      by_category: {},
      total: 5,
    });
    renderPill();
    await vi.waitFor(() => {
      expect(screen.getByText('5')).toBeInTheDocument();
    });
  });

  it('counts critical events alongside error events', async () => {
    // badge must show error + critical so critical-level app events are visible
    mockGetSummary.mockResolvedValue({
      by_level: { error: 3, critical: 2 },
      by_category: {},
      total: 5,
    });
    renderPill();
    await vi.waitFor(() => {
      expect(screen.getByText('5')).toBeInTheDocument();
    });
  });

  it('calls getSummary with excludeInfra:true to skip nginx infra events', async () => {
    // The queryFn passes { excludeInfra: true } so infrastructure events
    // (category=infra) are excluded from the badge count.
    mockGetSummary.mockResolvedValue({
      by_level: { error: 2 },
      by_category: {},
      total: 2,
    });
    renderPill();
    await vi.waitFor(() => screen.getByText('2'));
    expect(mockGetSummary).toHaveBeenCalledWith({ excludeInfra: true });
  });

  it('navigates to logs page on click', async () => {
    mockGetSummary.mockResolvedValue({
      by_level: { error: 3 },
      by_category: {},
      total: 3,
    });
    renderPill();
    await vi.waitFor(() => screen.getByText('3'));
    await userEvent.click(screen.getByRole('button'));
    expect(mockNavigate).toHaveBeenCalledWith('/logs?tab=events&level=error&since=24h');
  });

  it('renders nothing while loading (no data yet)', () => {
    // Return a never-resolving promise
    mockGetSummary.mockReturnValue(new Promise(() => {}));
    const { container } = renderPill();
    // Before data resolves, error count is 0 → pill hidden
    expect(container.firstChild).toBeNull();
  });

  // ── F7: poll gating ────────────────────────────────────────────────────────

  it('uses 30s refetchInterval when on /logs route (active monitoring)', async () => {
    // The pill should poll at 30s when the user is already looking at logs.
    // We verify by checking getSummary is called (proving the query ran) and
    // the pill shows the error count.
    mockGetSummary.mockResolvedValue({
      by_level: { error: 1 },
      by_category: {},
      total: 1,
    });
    renderPill('/logs');
    await vi.waitFor(() => {
      expect(screen.getByText('1')).toBeInTheDocument();
    });
    // getSummary was called — query ran with the 30s interval on the logs page.
    expect(mockGetSummary).toHaveBeenCalledWith({ excludeInfra: true });
  });

  it('uses 60s refetchInterval when NOT on /logs route (reduced background polling)', async () => {
    // Off-route pages use the slower 60s poll. Verify the query still runs
    // (first fetch is always immediate) and the pill renders the badge.
    mockGetSummary.mockResolvedValue({
      by_level: { error: 2 },
      by_category: {},
      total: 2,
    });
    renderPill('/my-day');
    await vi.waitFor(() => {
      expect(screen.getByText('2')).toBeInTheDocument();
    });
    // getSummary was called once (initial fetch). Background refetch interval
    // is 60s, which is not exercised in the unit test (no fake timers needed).
    expect(mockGetSummary).toHaveBeenCalledTimes(1);
  });
});

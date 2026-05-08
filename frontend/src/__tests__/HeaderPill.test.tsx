import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
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

const mockGetSummary = vi.mocked(getSummary);

function renderPill() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HeaderPill />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('HeaderPill', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
});

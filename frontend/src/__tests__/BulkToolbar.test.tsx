import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { BulkToolbar } from '@/components/feed/BulkToolbar';
import { useBulkSelection } from '@/stores/bulk-selection-store';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    bulkAction: vi.fn().mockResolvedValue({ succeeded: [1], failed: [] }),
  };
});

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

function renderToolbar(surface: Parameters<typeof BulkToolbar>[0]['surface']) {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <BulkToolbar surface={surface} />
    </QueryClientProvider>,
  );
}

describe('BulkToolbar', () => {
  beforeEach(() => {
    // Reset Zustand store between tests
    useBulkSelection.setState({ selectedIds: new Set() });
  });

  it('does not render when selectedIds is empty', () => {
    const { container } = renderToolbar('inbox');
    expect(container.firstChild).toBeNull();
  });

  it('renders sticky bar with count when selection exists', () => {
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    renderToolbar('inbox');
    expect(screen.getByText('1 selected')).toBeInTheDocument();
  });

  it('surface=inbox shows save, star, mark_read, dismiss actions', () => {
    useBulkSelection.setState({ selectedIds: new Set([1, 2]) });
    renderToolbar('inbox');
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Star' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Mark Read' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument();
  });

  it('surface=trash renders nothing (no bulk actions for trash)', () => {
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    const { container } = renderToolbar('trash');
    // SURFACE_ACTIONS.trash = [] → returns null because actions.length === 0
    expect(container.firstChild).toBeNull();
  });

  it('calls bulkAction with correct paper_ids and action on click', async () => {
    const { bulkAction } = await import('@/lib/api');
    useBulkSelection.setState({ selectedIds: new Set([3, 7]) });
    renderToolbar('inbox');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(bulkAction).toHaveBeenCalledWith({
      paper_ids: expect.arrayContaining([3, 7]),
      action: 'save',
    });
  });
});

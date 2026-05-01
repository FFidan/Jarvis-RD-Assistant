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

  // --- SURFACE_ACTIONS new verbs ---

  it('surface=inbox includes new lifecycle actions (save, skip, trash, feedback)', () => {
    useBulkSelection.setState({ selectedIds: new Set([1, 2]) });
    renderToolbar('inbox');
    expect(screen.getByRole('button', { name: /Save to Library/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Skip/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Move to Trash/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Star$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Unstar$/i })).toBeInTheDocument();
  });

  it('surface=library includes mark_reading and mark_done', () => {
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    renderToolbar('library');
    expect(screen.getByRole('button', { name: /Mark Reading/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Mark Done/i })).toBeInTheDocument();
  });

  it('surface=trash shows Restore action', () => {
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    renderToolbar('trash');
    expect(screen.getByRole('button', { name: /Restore/i })).toBeInTheDocument();
  });

  it('surface=search renders nothing (no bulk actions)', () => {
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    const { container } = renderToolbar('search');
    expect(container.firstChild).toBeNull();
  });

  it('surface=ask renders nothing (no bulk actions)', () => {
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    const { container } = renderToolbar('ask');
    expect(container.firstChild).toBeNull();
  });

  // --- Legacy stale verbs must NOT be present ---

  it('surface=inbox does NOT include legacy verbs (unsave, archive, unarchive, dismiss, mark_read)', () => {
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    renderToolbar('inbox');
    expect(screen.queryByRole('button', { name: /^Unsave$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Archive$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Unarchive$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Dismiss$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Mark Read$/i })).not.toBeInTheDocument();
  });

  // --- Mutation wiring ---

  it('calls bulkAction with correct paper_ids and action on click', async () => {
    const { bulkAction } = await import('@/lib/api');
    useBulkSelection.setState({ selectedIds: new Set([3, 7]) });
    renderToolbar('inbox');
    await userEvent.click(screen.getByRole('button', { name: /Save to Library/i }));
    expect(bulkAction).toHaveBeenCalledWith({
      paper_ids: expect.arrayContaining([3, 7]),
      action: 'save',
    });
  });

  it('calls bulkAction with mark_reading verb for library surface', async () => {
    const { bulkAction } = await import('@/lib/api');
    useBulkSelection.setState({ selectedIds: new Set([5]) });
    renderToolbar('library');
    await userEvent.click(screen.getByRole('button', { name: /Mark Reading/i }));
    expect(bulkAction).toHaveBeenCalledWith({
      paper_ids: [5],
      action: 'mark_reading',
    });
  });

  it('calls bulkAction with restore verb for trash surface', async () => {
    const { bulkAction } = await import('@/lib/api');
    useBulkSelection.setState({ selectedIds: new Set([9]) });
    renderToolbar('trash');
    await userEvent.click(screen.getByRole('button', { name: /Restore/i }));
    expect(bulkAction).toHaveBeenCalledWith({
      paper_ids: [9],
      action: 'restore',
    });
  });

  // --- NI-3 onError pattern ---

  it('shows toast.error with description on mutation error', async () => {
    const { bulkAction } = await import('@/lib/api');
    const { toast } = await import('sonner');
    vi.mocked(bulkAction).mockRejectedValueOnce(new Error('Network timeout'));
    useBulkSelection.setState({ selectedIds: new Set([1]) });
    renderToolbar('inbox');
    await userEvent.click(screen.getByRole('button', { name: /Save to Library/i }));
    // Allow microtasks to flush
    await new Promise((r) => setTimeout(r, 0));
    expect(toast.error).toHaveBeenCalledWith('Bulk action failed', {
      description: 'Network timeout',
    });
  });
});

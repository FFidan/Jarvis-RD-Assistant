/**
 * LifecycleActionsCard.test.tsx
 *
 * Regression-guard for the F2 fix: the 3-pane research-log layout dropped
 * PaperHeader, which silently removed reading-state / star / trash / restore /
 * hard-delete from Paper Detail. LifecycleActionsCard restores them in the
 * right action rail. These tests assert the state-contextual buttons render
 * and call the correct mutations + query invalidation.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { LifecycleActionsCard } from '@/components/paper/LifecycleActionsCard';
import type { LifecycleState } from '@/types';
import { createTestQueryClient } from '@/__tests__/test-utils';
import { useResearchMilestoneStore } from '@/stores/research-milestone-store';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    savePaper: vi.fn().mockResolvedValue({}),
    skipPaper: vi.fn().mockResolvedValue({}),
    markReading: vi.fn().mockResolvedValue({}),
    markDone: vi.fn().mockResolvedValue({}),
    trashPaper: vi.fn().mockResolvedValue({}),
    restorePaper: vi.fn().mockResolvedValue({}),
    starPaper: vi.fn().mockResolvedValue({}),
    unstarPaper: vi.fn().mockResolvedValue({}),
    hardDeletePaper: vi.fn().mockResolvedValue({ deleted: 1 }),
  };
});

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

function renderCard(
  state: LifecycleState = 'inbox',
  starred = false,
) {
  const queryClient = createTestQueryClient();
  const result = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <LifecycleActionsCard
          paperId={42}
          paperTitle="Attention Is All You Need"
          state={state}
          starred={starred}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, queryClient };
}

describe('LifecycleActionsCard — state-contextual rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders Save + Skip + Star + Trash for inbox state', () => {
    renderCard('inbox');
    expect(screen.getByRole('button', { name: /Save paper/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Skip paper/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Star paper/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Trash paper/ })).toBeInTheDocument();
  });

  it('renders Start Reading for to_read state', () => {
    renderCard('to_read');
    expect(screen.getByRole('button', { name: /Mark as reading/ })).toBeInTheDocument();
  });

  it('renders Mark Done + Pause reading for reading state', () => {
    renderCard('reading');
    expect(screen.getByRole('button', { name: /Mark as done/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Pause reading/ })).toBeInTheDocument();
  });

  it('renders Resume reading for done state', () => {
    renderCard('done');
    expect(screen.getByRole('button', { name: /Resume reading/ })).toBeInTheDocument();
  });

  it('renders Restore + Permanently delete for trash state, no Star/Trash', () => {
    renderCard('trash');
    expect(screen.getByRole('button', { name: /Restore paper/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Permanently delete paper/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Star paper/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Trash paper/ })).not.toBeInTheDocument();
  });
});

describe('LifecycleActionsCard — mutation calls', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useResearchMilestoneStore.setState({
      completed: { save: false, analyze: false },
      advancedCueDismissed: false,
    });
  });

  it('calls savePaper when Save clicked (inbox)', async () => {
    const { savePaper } = await import('@/lib/api');
    const user = userEvent.setup();
    renderCard('inbox');
    await user.click(screen.getByRole('button', { name: /Save paper/ }));
    await waitFor(() => expect(savePaper).toHaveBeenCalledWith(42));
    expect(useResearchMilestoneStore.getState().completed.save).toBe(true);
  });

  it('does not record the Save milestone when savePaper fails', async () => {
    const { savePaper } = await import('@/lib/api');
    vi.mocked(savePaper).mockRejectedValueOnce(new Error('save failed'));
    const user = userEvent.setup();
    renderCard('inbox');

    await user.click(screen.getByRole('button', { name: /Save paper/ }));
    await waitFor(() => expect(savePaper).toHaveBeenCalledWith(42));

    expect(useResearchMilestoneStore.getState().completed.save).toBe(false);
  });

  it('calls markReading when Start Reading clicked (to_read)', async () => {
    const { markReading } = await import('@/lib/api');
    const user = userEvent.setup();
    renderCard('to_read');
    await user.click(screen.getByRole('button', { name: /Mark as reading/ }));
    await waitFor(() => expect(markReading).toHaveBeenCalledWith(42));
  });

  it('calls markDone when Mark Done clicked (reading)', async () => {
    const { markDone } = await import('@/lib/api');
    const user = userEvent.setup();
    renderCard('reading');
    await user.click(screen.getByRole('button', { name: /Mark as done/ }));
    await waitFor(() => expect(markDone).toHaveBeenCalledWith(42));
  });

  it('calls restorePaper when Restore clicked (trash)', async () => {
    const { restorePaper } = await import('@/lib/api');
    const user = userEvent.setup();
    renderCard('trash');
    await user.click(screen.getByRole('button', { name: /Restore paper/ }));
    await waitFor(() => expect(restorePaper).toHaveBeenCalledWith(42));
  });

  it('calls starPaper when star clicked and not starred', async () => {
    const { starPaper } = await import('@/lib/api');
    const user = userEvent.setup();
    renderCard('inbox', false);
    await user.click(screen.getByRole('button', { name: /Star paper/ }));
    await waitFor(() => expect(starPaper).toHaveBeenCalledWith(42));
  });

  it('calls unstarPaper when star clicked and already starred', async () => {
    const { unstarPaper } = await import('@/lib/api');
    const user = userEvent.setup();
    renderCard('inbox', true);
    await user.click(screen.getByRole('button', { name: /Starred/ }));
    await waitFor(() => expect(unstarPaper).toHaveBeenCalledWith(42));
  });

  it('calls skipPaper when Skip clicked (inbox)', async () => {
    const { skipPaper } = await import('@/lib/api');
    const user = userEvent.setup();
    renderCard('inbox');
    await user.click(screen.getByRole('button', { name: /Skip paper/ }));
    await waitFor(() => expect(skipPaper).toHaveBeenCalledWith(42));
  });

  it('trash confirm toast triggers trashPaper', async () => {
    const { trashPaper } = await import('@/lib/api');
    const { toast } = await import('sonner');
    const user = userEvent.setup();
    renderCard('inbox');
    await user.click(screen.getByRole('button', { name: /Trash paper/ }));
    // handleTrash fires a confirm toast; invoke its action like PaperHeader does
    const warnCall = vi.mocked(toast.warning).mock.calls[0];
    expect(warnCall).toBeDefined();
    expect(warnCall![0]).toBe('Move to Trash?');
    const action = (warnCall![1] as unknown as { action: { onClick: () => void } }).action;
    action.onClick();
    await waitFor(() => expect(trashPaper).toHaveBeenCalledWith(42));
  });
});

describe('LifecycleActionsCard — query invalidation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('invalidates papers-feed, feed-counts and paper-detail after save', async () => {
    const { savePaper } = await import('@/lib/api');
    vi.mocked(savePaper).mockResolvedValue({} as never);
    const user = userEvent.setup();
    const { queryClient } = renderCard('inbox');
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    await user.click(screen.getByRole('button', { name: /Save paper/ }));

    await waitFor(() => {
      const keys = invalidateSpy.mock.calls.map(
        ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
      );
      expect(keys).toContainEqual(['papers-feed']);
      expect(keys).toContainEqual(['feed-counts']);
      expect(keys).toContainEqual(['paper-detail', 42]);
    });
  });
});

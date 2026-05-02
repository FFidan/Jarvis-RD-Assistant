import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import {
  fetchFeed,
  savePaper,
  skipPaper,
  markReading,
  markDone,
  trashPaper,
  restorePaper,
  starPaper,
  unstarPaper,
} from '@/lib/api';
import type { FeedPaper, SurfaceView } from '@/types';
import { FeedPaperRow } from './FeedPaperRow';
import { BulkToolbar } from './BulkToolbar';
import { HardDeleteModal } from './HardDeleteModal';
import { useBulkSelection } from '@/stores/bulk-selection-store';
import { useFeedKeyboardShortcuts } from '@/hooks/useFeedKeyboardShortcuts';
import { useKeyboardShortcuts } from '@/stores/keyboard-shortcuts-store';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/ui/skeleton';
import { PaginationControls, PAGE_SIZE_OPTIONS } from './PaginationControls';
import type { PageSize } from './PaginationControls';
import { Inbox, Library, Star, BookOpen, Trash2 } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

// NI-3: shared toastError helper — every mutation must call this on error
const toastError = (verb: string) => (err: unknown) =>
  toast.error(`Failed to ${verb}`, { description: err instanceof Error ? err.message : 'Unknown error' });

interface FeedViewProps {
  surface: SurfaceView;
  /** Library sub-chip filter. 'pulse-this-week' is passed through to the backend as-is. */
  filter?: 'starred' | 'reading' | 'to_read' | 'done' | 'pulse-this-week' | null;
}

// Per-surface empty state copy
const EMPTY_STATE: Record<string, { icon: LucideIcon; title: string; description: string }> = {
  inbox: {
    icon: Inbox,
    title: 'Inbox is empty',
    description: 'Auto-fetched papers will appear here. Add topics in Settings to discover research automatically.',
  },
  library: {
    icon: Library,
    title: 'No papers in your library',
    description: 'Save papers from the Inbox to build your library.',
  },
  starred: {
    icon: Star,
    title: 'No starred papers',
    description: 'Star papers to keep track of your favourites.',
  },
  reading: {
    icon: BookOpen,
    title: 'Nothing in reading list',
    description: 'Mark papers as "Reading" on the detail page to track your in-progress reads.',
  },
  trash: {
    icon: Trash2,
    title: 'Trash is empty',
    description: 'Trashed papers land here. You can restore them or delete them permanently.',
  },
};

function getEmptyState(surface: SurfaceView) {
  return EMPTY_STATE[surface] ?? EMPTY_STATE.library;
}

const DEFAULT_LIMIT: PageSize = 30;

export function FeedView({ surface, filter }: FeedViewProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  // Pagination state — persisted in URL search params for deep-link support
  const rawLimit = Number(searchParams.get('limit'));
  const limit: PageSize = (PAGE_SIZE_OPTIONS as readonly number[]).includes(rawLimit)
    ? (rawLimit as PageSize)
    : DEFAULT_LIMIT;
  const offset = Math.max(0, Number(searchParams.get('offset')) || 0);

  const setPagination = useCallback(
    (newOffset: number, newLimit: PageSize) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set('limit', String(newLimit));
          next.set('offset', String(newOffset));
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const lastSurfaceFilterRef = useRef<string>(`${surface}|${filter ?? ''}`);
  useEffect(() => {
    const key = `${surface}|${filter ?? ''}`;
    if (lastSurfaceFilterRef.current !== key) {
      lastSurfaceFilterRef.current = key;
      if (offset !== 0) setPagination(0, limit);
    }
  }, [surface, filter, offset, limit, setPagination]);

  // Keyboard-navigation focused row index (j/k)
  const [focusedIdx, setFocusedIdx] = useState<number>(0);

  // Hard-delete modal state
  const [hardDeleteTarget, setHardDeleteTarget] = useState<{ id: number; title: string } | null>(null);

  // Bulk selection (subscribe to selectedIds for re-renders; mutate via getState())
  const selectedIds = useBulkSelection((s) => s.selectedIds);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['papers-feed', surface, filter, limit, offset],
    // fetchFeed accepts SurfaceView string
    queryFn: () => fetchFeed({ view: surface as Parameters<typeof fetchFeed>[0]['view'], filter, limit, offset }),
  });

  // Cast to FeedPaper[] — backend (Wave 1ab) already returns the Phase-A shape
  const papers = (data?.papers ?? []) as FeedPaper[];

  const invalidateFeed = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
    void queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
  }, [queryClient]);

  // --- Lifecycle mutations (9 mutations, all with NI-3 onError) ---

  const saveMutation = useMutation({
    mutationFn: (paperId: number) => savePaper(paperId),
    onSuccess: invalidateFeed,
    onError: toastError('save paper'),
  });

  const skipMutation = useMutation({
    mutationFn: skipPaper,
    onSuccess: invalidateFeed,
    onError: toastError('skip paper'),
  });

  const markReadingMut = useMutation({
    mutationFn: markReading,
    onSuccess: invalidateFeed,
    onError: toastError('mark reading'),
  });

  const markDoneMut = useMutation({
    mutationFn: markDone,
    onSuccess: invalidateFeed,
    onError: toastError('mark done'),
  });

  const trashMutation = useMutation({
    mutationFn: trashPaper,
    onSuccess: invalidateFeed,
    onError: toastError('trash paper'),
  });

  const starMutation = useMutation({
    mutationFn: starPaper,
    onSuccess: invalidateFeed,
    onError: toastError('star paper'),
  });

  const unstarMutation = useMutation({
    mutationFn: unstarPaper,
    onSuccess: invalidateFeed,
    onError: toastError('unstar paper'),
  });

  const restoreMutation = useMutation({
    mutationFn: restorePaper,
    onSuccess: invalidateFeed,
    onError: toastError('restore paper'),
  });


  // --- Navigation ---

  const onView = useCallback(
    (paperId: number) => {
      navigate(`/paper/${paperId}`);
    },
    [navigate],
  );

  // --- Hard-delete modal helpers ---

  const openHardDelete = useCallback((paperId: number, title: string) => {
    setHardDeleteTarget({ id: paperId, title });
  }, []);

  // ── Keyboard shortcuts (j/k navigation + surface-aware row actions) ───────

  // Clamp focusedIdx to valid range so the hook always gets a valid index or null
  const clampedFocusedIdx = papers.length === 0 ? null : Math.min(focusedIdx, papers.length - 1);

  const shortcutCallbacks = useMemo(
    () => ({
      onNext: () => {
        if (papers.length === 0) return;
        setFocusedIdx((i) => Math.min(i + 1, papers.length - 1));
      },
      onPrev: () => {
        if (papers.length === 0) return;
        setFocusedIdx((i) => Math.max(i - 1, 0));
      },
      onSave: (id: number) => saveMutation.mutate(id),
      onSkip: (id: number) => skipMutation.mutate(id),
      onMarkReading: (id: number) => markReadingMut.mutate(id),
      onMarkDone: (id: number) => markDoneMut.mutate(id),
      // setAside: reading → to_read via /save endpoint
      onSetAside: (id: number) => saveMutation.mutate(id),
      onTrash: (id: number) => trashMutation.mutate(id),
      onStar: (id: number) => starMutation.mutate(id),
      onUnstar: (id: number) => unstarMutation.mutate(id),
      onRestore: (id: number) => restoreMutation.mutate(id),
      // Shift+S: save then star (chained)
      onSaveAndStar: (id: number) => {
        saveMutation.mutate(id);
        starMutation.mutate(id);
      },
      onOpenDetail: (id: number) => onView(id),
      onShowCheatSheet: () => useKeyboardShortcuts.getState().open(),
      onClearSelection: () => useBulkSelection.getState().clear(),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [papers.length, saveMutation, skipMutation, markReadingMut, markDoneMut, trashMutation, starMutation, unstarMutation, restoreMutation, onView],
  );

  useFeedKeyboardShortcuts(surface, papers, clampedFocusedIdx, shortcutCallbacks);

  // --- Render ---

  if (isLoading) {
    return (
      <div className="space-y-4 pt-4">
        {[1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-32 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <div className="py-8 text-center">
        <p className="text-sm text-destructive">
          Failed to load papers: {(error as Error).message}
        </p>
      </div>
    );
  }

  const emptyState = getEmptyState(surface);

  return (
    <>
      <BulkToolbar surface={surface} />

      <div className="space-y-4 pt-4">
        {papers.length === 0 ? (
          <EmptyState
            icon={emptyState.icon}
            title={emptyState.title}
            description={emptyState.description}
          />
        ) : (
          <>
            <PaginationControls
              offset={offset}
              limit={limit}
              total={data?.total ?? papers.length}
              onChange={setPagination}
            />

            {papers.map((paper) => (
              <FeedPaperRow
                key={paper.id}
                paper={paper}
                surface={surface}
                isSelected={selectedIds.has(paper.id)}
                onToggleSelect={(id) => useBulkSelection.getState().toggle(id)}
                // Lifecycle callbacks wired to mutations
                onSave={(id) => saveMutation.mutate(id)}
                onSkip={(id) => skipMutation.mutate(id)}
                onMarkReading={(id) => markReadingMut.mutate(id)}
                onMarkDone={(id) => markDoneMut.mutate(id)}
                // setAside: /save sets state='to_read' unconditionally (grounded: routers/papers.py:527)
                onSetAside={(id) => saveMutation.mutate(id)}
                // reopen: Done → reading
                onReopen={(id) => markReadingMut.mutate(id)}
                onTrash={(id) => trashMutation.mutate(id)}
                onStar={(id) => starMutation.mutate(id)}
                onUnstar={(id) => unstarMutation.mutate(id)}
                onRestore={(id) => restoreMutation.mutate(id)}
                onHardDelete={(id) => openHardDelete(id, paper.title)}
                onView={onView}
                viewLabel="View Details"
              />
            ))}
          </>
        )}
      </div>

      {/* Hard-delete confirmation modal */}
      {hardDeleteTarget && (
        <HardDeleteModal
          open={hardDeleteTarget !== null}
          onOpenChange={(open) => {
            if (!open) setHardDeleteTarget(null);
          }}
          paperId={hardDeleteTarget.id}
          paperTitle={hardDeleteTarget.title}
          onDeleted={() => {
            setHardDeleteTarget(null);
          }}
        />
      )}
    </>
  );
}

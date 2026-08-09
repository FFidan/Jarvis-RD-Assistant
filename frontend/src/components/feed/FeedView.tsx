import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import { errorMessage } from '@/lib/errors';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
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
import type { FeedPaper, FeedScope, SurfaceView } from '@/types';
import { FeedPaperRow } from './FeedPaperRow';
import { BulkToolbar } from './BulkToolbar';
import { HardDeleteModal } from './HardDeleteModal';
import { useBulkSelection } from '@/stores/bulk-selection-store';
import { useFeedKeyboardShortcuts } from '@/hooks/useFeedKeyboardShortcuts';
import { useKeyboardShortcuts } from '@/stores/keyboard-shortcuts-store';
import { useResearchMilestoneStore } from '@/stores/research-milestone-store';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/ui/skeleton';
import { PaginationControls, PAGE_SIZE_OPTIONS } from './PaginationControls';
import type { PageSize } from './PaginationControls';
import { Inbox, Library, Star, BookOpen, Trash2 } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

// NI-3: shared toastError helper — every mutation must call this on error
const toastError = (verb: string) => (err: unknown) =>
  toast.error(`Failed to ${verb}`, { description: errorMessage(err) });

interface FeedViewProps {
  surface: SurfaceView;
  /** Library sub-chip filter. 'pulse-this-week' is passed through to the backend as-is. */
  filter?: 'starred' | 'reading' | 'to_read' | 'done' | 'pulse-this-week' | null;
  scope?: FeedScope;
  /** Inbox source-type chip filter — null/undefined means all sources. */
  sourceTypes?: string | null;
  /** §Topic facet — restrict the feed to papers tagged with this topic id. */
  topicId?: number | null;
  /** §Topic facet "Untagged" sentinel — restrict to papers with no topic. */
  untagged?: boolean;
  /**
   * Scoped list-filter: client-side title/author text filter
   * applied to the currently loaded page. Not sent to the backend.
   */
  listFilter?: string;
}

// Per-surface empty state copy
type EmptyStateCopy = { icon: LucideIcon; title: string; description: string };

const LIBRARY_EMPTY_STATE: EmptyStateCopy = {
  icon: Library,
  title: 'No papers in your library',
  description: 'Save papers from the Inbox to build your library.',
};

const EMPTY_STATE: Record<string, EmptyStateCopy> = {
  inbox: {
    icon: Inbox,
    title: 'Inbox is empty',
    description: 'Papers from your sources will appear here. Add topics in Settings to discover research automatically.',
  },
  library: LIBRARY_EMPTY_STATE,
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

function getEmptyState(surface: SurfaceView): EmptyStateCopy {
  return EMPTY_STATE[surface] ?? LIBRARY_EMPTY_STATE;
}

const DEFAULT_LIMIT: PageSize = 30;

export function FeedView({ surface, filter, scope = 'library', sourceTypes, topicId, untagged, listFilter }: FeedViewProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const recordResearchMilestone = useResearchMilestoneStore(
    (state) => state.recordMilestone,
  );

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

  const serverSearchSurface = surface === 'library' || surface === 'inbox';
  const [serverQuery, setServerQuery] = useState<string>('');
  useEffect(() => {
    const trimmed = listFilter?.trim() ?? '';
    if (!serverSearchSurface || trimmed.length < 3) {
      setServerQuery('');
      return;
    }
    const id = setTimeout(() => setServerQuery(trimmed), 300);
    return () => clearTimeout(id);
  }, [listFilter, serverSearchSurface]);

  const lastSurfaceFilterRef = useRef<string>(`${surface}|${filter ?? ''}|${scope}|${topicId ?? ''}|${untagged ? '1' : ''}|${sourceTypes ?? ''}|`);
  useEffect(() => {
    const key = `${surface}|${filter ?? ''}|${scope}|${topicId ?? ''}|${untagged ? '1' : ''}|${sourceTypes ?? ''}|${serverQuery}`;
    if (lastSurfaceFilterRef.current !== key) {
      lastSurfaceFilterRef.current = key;
      if (offset !== 0) setPagination(0, limit);
    }
  }, [surface, filter, scope, topicId, untagged, sourceTypes, serverQuery, offset, limit, setPagination]);

  // Keyboard-navigation focused row index (j/k)
  const [focusedIdx, setFocusedIdx] = useState<number>(0);

  // Hard-delete modal state
  const [hardDeleteTarget, setHardDeleteTarget] = useState<{ id: number; title: string } | null>(null);

  // Bulk selection (subscribe to selectedIds for re-renders; mutate via getState())
  const selectedIds = useBulkSelection((s) => s.selectedIds);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: [...QUERY_KEYS.papers.feed(surface, filter ?? '', scope, limit, offset, sourceTypes ? [sourceTypes] : null, topicId ?? null, untagged ?? false), serverQuery],
    // fetchFeed accepts SurfaceView string
    queryFn: () => fetchFeed({ view: surface as Parameters<typeof fetchFeed>[0]['view'], filter, scope, limit, offset, sourceTypes, topicId, untagged, q: serverQuery || undefined }),
    placeholderData: keepPreviousData,
  });

  // Client-side scoped list-filter (title/author, within active facets)
  const papers = useMemo(() => {
    // Cast inside memo so the expression doesn't escape and destabilise deps
    const raw = (data?.papers ?? []) as FeedPaper[];
    // Server-side search (q) already matched title/abstract/authors with
    // stemming — re-filtering by substring here would hide legitimate hits.
    if (!listFilter || serverQuery) return raw;
    const q = listFilter.toLowerCase();
    return raw.filter(
      (p) =>
        p.title.toLowerCase().includes(q) ||
        p.authors.some((a) => a.toLowerCase().includes(q)),
    );
  }, [data?.papers, listFilter, serverQuery]);

  const invalidateFeed = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.feedAll() });
    void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.feed.counts() });
  }, [queryClient]);

  // --- Lifecycle mutations (9 mutations, all with NI-3 onError) ---

  const saveMutation = useMutation({
    mutationFn: (paperId: number) => savePaper(paperId),
    onSuccess: () => {
      recordResearchMilestone('save');
      invalidateFeed();
    },
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

  // --- Stable callbacks for FeedPaperRow props ---
  // Inline arrows create new function references each render and break React.memo.
  // These useCallbacks depend only on their respective mutation refs which are
  // stable across renders (TanStack Query mutations are stable objects).

  const onToggleSelectCb = useCallback(
    (id: number) => useBulkSelection.getState().toggle(id),
    [],
  );
  const onSaveCb = useCallback((id: number) => saveMutation.mutate(id), [saveMutation]);
  const onSkipCb = useCallback((id: number) => skipMutation.mutate(id), [skipMutation]);
  const onMarkReadingCb = useCallback((id: number) => markReadingMut.mutate(id), [markReadingMut]);
  const onMarkDoneCb = useCallback((id: number) => markDoneMut.mutate(id), [markDoneMut]);
  const onSetAsideCb = useCallback((id: number) => saveMutation.mutate(id), [saveMutation]);
  const onReopenCb = useCallback((id: number) => markReadingMut.mutate(id), [markReadingMut]);
  const onTrashCb = useCallback((id: number) => trashMutation.mutate(id), [trashMutation]);
  const onStarCb = useCallback((id: number) => starMutation.mutate(id), [starMutation]);
  const onUnstarCb = useCallback((id: number) => unstarMutation.mutate(id), [unstarMutation]);
  const onRestoreCb = useCallback((id: number) => restoreMutation.mutate(id), [restoreMutation]);

  // Stable onHardDelete callback — avoids inline arrow that creates a new
  // function reference every parent render and defeats React.memo on FeedPaperRow.
  // We keep a ref to the latest `papers` array so the callback can look up the
  // title at click time without being included in the dependency array.
  const papersRef = useRef(papers);
  useEffect(() => { papersRef.current = papers; }, [papers]);
  const onHardDeleteCb = useCallback(
    (id: number) => {
      const p = papersRef.current.find((r) => r.id === id);
      if (p) openHardDelete(id, p.title);
    },
    [openHardDelete],
  );

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
    [papers, saveMutation, skipMutation, markReadingMut, markDoneMut, trashMutation, starMutation, unstarMutation, restoreMutation, onView],
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
          Failed to load papers: {errorMessage(error)}
        </p>
      </div>
    );
  }

  const emptyState = getEmptyState(surface);

  return (
    <>
      <BulkToolbar surface={surface} papersOnPage={papers.map((p) => p.id)} />

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
                onToggleSelect={onToggleSelectCb}
                // Lifecycle callbacks wired to mutations (stable via useCallback)
                onSave={onSaveCb}
                onSkip={onSkipCb}
                onMarkReading={onMarkReadingCb}
                onMarkDone={onMarkDoneCb}
                // setAside: /save sets state='to_read' unconditionally (grounded: routers/papers.py:527)
                onSetAside={onSetAsideCb}
                // reopen: Done → reading
                onReopen={onReopenCb}
                onTrash={onTrashCb}
                onStar={onStarCb}
                onUnstar={onUnstarCb}
                onRestore={onRestoreCb}
                onHardDelete={onHardDeleteCb}
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

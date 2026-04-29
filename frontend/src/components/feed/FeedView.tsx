import { useState, useCallback } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import {
  fetchFeed,
  savePaper,
  unsavePaper,
  dismissPaper,
  restorePaper,
  hardDeletePaper,
  markPaperRead,
  bookmarkPaper,
  archivePaper,
} from '@/lib/api';
import type { SurfaceView } from '@/types';
import { FeedPaperRow } from './FeedPaperRow';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Inbox, Library, Star, Archive, BookOpen, Trash2 } from 'lucide-react';

interface FeedViewProps {
  surface: SurfaceView;
  filter?: 'starred' | 'archived' | 'reading' | 'pulse-this-week' | null;
}

// Per-surface empty state copy
const EMPTY_STATE: Record<string, { icon: React.ElementType; title: string; description: string }> = {
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
  archived: {
    icon: Archive,
    title: 'No archived papers',
    description: 'Archive papers to remove them from your active feed without deleting them.',
  },
  reading: {
    icon: BookOpen,
    title: 'Nothing in reading list',
    description: 'Mark papers as "Reading" on the detail page to track your in-progress reads.',
  },
  trash: {
    icon: Trash2,
    title: 'Trash is empty',
    description: 'Dismissed papers land here. You can restore them or delete them permanently.',
  },
};

function getEmptyState(surface: SurfaceView) {
  return EMPTY_STATE[surface] ?? EMPTY_STATE.library;
}

export function FeedView({ surface, filter }: FeedViewProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Per-row pending state tracking
  const [pendingSave, setPendingSave] = useState<Set<number>>(new Set());
  const [pendingMarkRead, setPendingMarkRead] = useState<Set<number>>(new Set());
  const [pendingStar, setPendingStar] = useState<Set<number>>(new Set());
  const [pendingArchive, setPendingArchive] = useState<Set<number>>(new Set());
  const [pendingDismiss, setPendingDismiss] = useState<Set<number>>(new Set());
  const [pendingRestore, setPendingRestore] = useState<Set<number>>(new Set());

  // Hard-delete modal state
  const [hardDeleteTarget, setHardDeleteTarget] = useState<{ id: number; title: string } | null>(null);
  const [hardDeletePending, setHardDeletePending] = useState(false);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['papers-feed', surface, filter],
    queryFn: () => fetchFeed({ view: surface, filter }),
  });

  const papers = data?.papers ?? [];

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
    void queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
  }, [queryClient]);

  // --- Mutation helpers ---

  const saveMutation = useMutation({
    mutationFn: (paperId: number) => savePaper(paperId, { star: false }),
    onSuccess: invalidate,
  });

  const saveAndStarMutation = useMutation({
    mutationFn: (paperId: number) => savePaper(paperId, { star: true }),
    onSuccess: invalidate,
  });

  const unsaveMutation = useMutation({
    mutationFn: unsavePaper,
    onSuccess: invalidate,
  });

  const markReadMutation = useMutation({
    mutationFn: markPaperRead,
    onSuccess: invalidate,
  });

  const starMutation = useMutation({
    mutationFn: bookmarkPaper,
    onSuccess: invalidate,
  });

  const archiveMutation = useMutation({
    mutationFn: archivePaper,
    onSuccess: invalidate,
  });

  const dismissMutation = useMutation({
    mutationFn: (paperId: number) => dismissPaper(paperId),
    onSuccess: invalidate,
  });

  const restoreMutation = useMutation({
    mutationFn: restorePaper,
    onSuccess: invalidate,
  });

  const hardDeleteMutation = useMutation({
    mutationFn: ({ id, title }: { id: number; title: string }) =>
      hardDeletePaper(id, { confirm_title: title }),
    onSuccess: () => {
      setHardDeleteTarget(null);
      setHardDeletePending(false);
      invalidate();
    },
    onError: () => {
      setHardDeletePending(false);
    },
  });

  // --- Per-row action wrappers (track pending per paperId) ---

  const addPending = (set: React.Dispatch<React.SetStateAction<Set<number>>>, id: number) =>
    set((prev) => new Set(prev).add(id));
  const removePending = (set: React.Dispatch<React.SetStateAction<Set<number>>>, id: number) =>
    set((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });

  const onSave = useCallback(
    (paperId: number) => {
      addPending(setPendingSave, paperId);
      saveMutation.mutate(paperId, { onSettled: () => removePending(setPendingSave, paperId) });
    },
    [saveMutation],
  );

  const onSaveAndStar = useCallback(
    (paperId: number) => {
      addPending(setPendingSave, paperId);
      saveAndStarMutation.mutate(paperId, { onSettled: () => removePending(setPendingSave, paperId) });
    },
    [saveAndStarMutation],
  );

  const onUnsave = useCallback(
    (paperId: number) => {
      addPending(setPendingSave, paperId);
      unsaveMutation.mutate(paperId, { onSettled: () => removePending(setPendingSave, paperId) });
    },
    [unsaveMutation],
  );

  const onMarkRead = useCallback(
    (paperId: number) => {
      addPending(setPendingMarkRead, paperId);
      markReadMutation.mutate(paperId, { onSettled: () => removePending(setPendingMarkRead, paperId) });
    },
    [markReadMutation],
  );

  const onStar = useCallback(
    (paperId: number) => {
      addPending(setPendingStar, paperId);
      starMutation.mutate(paperId, { onSettled: () => removePending(setPendingStar, paperId) });
    },
    [starMutation],
  );

  const onArchive = useCallback(
    (paperId: number) => {
      addPending(setPendingArchive, paperId);
      archiveMutation.mutate(paperId, { onSettled: () => removePending(setPendingArchive, paperId) });
    },
    [archiveMutation],
  );

  const onDismiss = useCallback(
    (paperId: number) => {
      addPending(setPendingDismiss, paperId);
      dismissMutation.mutate(paperId, { onSettled: () => removePending(setPendingDismiss, paperId) });
    },
    [dismissMutation],
  );

  const onRestore = useCallback(
    (paperId: number) => {
      addPending(setPendingRestore, paperId);
      restoreMutation.mutate(paperId, { onSettled: () => removePending(setPendingRestore, paperId) });
    },
    [restoreMutation],
  );

  const onHardDelete = useCallback((paperId: number, title: string) => {
    setHardDeleteTarget({ id: paperId, title });
  }, []);

  const onView = useCallback(
    (paperId: number) => {
      navigate(`/paper/${paperId}`);
    },
    [navigate],
  );

  const confirmHardDelete = useCallback(() => {
    if (!hardDeleteTarget) return;
    setHardDeletePending(true);
    hardDeleteMutation.mutate({ id: hardDeleteTarget.id, title: hardDeleteTarget.title });
  }, [hardDeleteTarget, hardDeleteMutation]);

  // --- Surface-aware callback sets ---

  function rowCallbacks(paperId: number) {
    const base = {
      onMarkRead,
      onView,
      markReadPending: pendingMarkRead.has(paperId),
      archivePending: pendingArchive.has(paperId),
    };

    if (surface === 'inbox') {
      return {
        ...base,
        onSave: () => onSave(paperId),
        onSaveAndStar: () => onSaveAndStar(paperId),
        onDismiss: () => onDismiss(paperId),
        savePending: pendingSave.has(paperId),
        dismissPending: pendingDismiss.has(paperId),
      };
    }

    if (surface === 'trash') {
      return {
        onView,
        onRestore: () => onRestore(paperId),
        onHardDelete: (title: string) => onHardDelete(paperId, title),
        restorePending: pendingRestore.has(paperId),
      };
    }

    // library / starred / archived / reading
    return {
      ...base,
      onStar: () => onStar(paperId),
      onArchive: surface !== 'archived' ? () => onArchive(paperId) : undefined,
      onUnarchive: surface === 'archived' ? () => onArchive(paperId) : undefined,
      onUnsave: () => onUnsave(paperId),
      onDismiss: () => onDismiss(paperId),
      starPending: pendingStar.has(paperId),
      dismissPending: pendingDismiss.has(paperId),
    };
  }

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
      <div className="space-y-4 pt-4">
        {papers.length === 0 ? (
          <EmptyState
            icon={emptyState.icon}
            title={emptyState.title}
            description={emptyState.description}
          />
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              {data?.total != null
                ? `Showing ${papers.length} of ${data.total} papers`
                : `${papers.length} papers`}
            </p>

            {papers.map((paper) => {
              const callbacks = rowCallbacks(paper.id);
              return (
                <FeedPaperRow
                  key={paper.id}
                  paper={paper}
                  onMarkRead={'onMarkRead' in callbacks ? callbacks.onMarkRead : undefined}
                  markReadPending={'markReadPending' in callbacks ? callbacks.markReadPending : false}
                  onArchive={'onArchive' in callbacks ? callbacks.onArchive : undefined}
                  archivePending={'archivePending' in callbacks ? callbacks.archivePending : false}
                  onView={callbacks.onView}
                  viewLabel="View Details"
                />
              );
            })}
          </>
        )}
      </div>

      {/* Hard-delete confirmation modal */}
      <Dialog
        open={hardDeleteTarget !== null}
        onOpenChange={(open) => {
          if (!open && !hardDeletePending) setHardDeleteTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Permanently delete paper?</DialogTitle>
            <DialogDescription>
              This will remove{' '}
              <span className="font-medium">{hardDeleteTarget?.title}</span> and all
              associated data. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setHardDeleteTarget(null)}
              disabled={hardDeletePending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={confirmHardDelete}
              disabled={hardDeletePending}
            >
              {hardDeletePending ? 'Deleting…' : 'Delete permanently'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

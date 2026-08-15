/**
 * LifecycleActionsCard — paper lifecycle actions for the Paper Detail toolbar.
 *
 * Extracted from PaperHeader's action cluster when the reading-first layout
 * dropped PaperHeader, which would otherwise have silently removed
 * reading-state / star / trash / restore / hard-delete from Paper Detail.
 *
 * Behaviour mirrors PaperHeader exactly:
 *  - state-contextual primary/secondary buttons (inbox / to_read / reading /
 *    done / trash),
 *  - star toggle + Trash shown on every non-trash state,
 *  - identical query invalidation (papers-feed / feed-counts / paper-detail)
 *    and NI-3 error toasts,
 *  - trash uses the "Move to Trash?" confirm toast; hard-delete opens the
 *    HardDeleteModal on the trash surface.
 */
import { useState } from 'react';
import type { LifecycleState } from '@/types';
import { Button } from '@/components/ui/button';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { HardDeleteModal } from '@/components/feed/HardDeleteModal';
import {
  Star,
  StarOff,
  Save,
  SkipForward,
  BookOpen,
  Library,
  CheckCircle,
  RotateCcw,
  ArchiveRestore,
  Trash2,
  Trash,
} from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  savePaper,
  skipPaper,
  markReading,
  markDone,
  trashPaper,
  restorePaper,
  starPaper,
  unstarPaper,
  hardDeletePaper,
} from '@/lib/api';
import { toast } from 'sonner';
import { errorMessage } from '@/lib/errors';
import { useResearchMilestoneStore } from '@/stores/research-milestone-store';

interface LifecycleActionsCardProps {
  paperId: number;
  paperTitle: string;
  /** Current lifecycle state. Defaults to 'inbox' when unknown. */
  state?: LifecycleState;
  /** Whether the paper is starred. */
  starred?: boolean;
}

export function LifecycleActionsCard({
  paperId,
  paperTitle,
  state = 'inbox',
  starred = false,
}: LifecycleActionsCardProps) {
  const [hardDeleteOpen, setHardDeleteOpen] = useState(false);
  const queryClient = useQueryClient();
  const recordResearchMilestone = useResearchMilestoneStore(
    (store) => store.recordMilestone,
  );

  // NI-3 error helper (identical to PaperHeader)
  const toastError = (verb: string) => (err: unknown) =>
    toast.error(`Failed to ${verb}`, {
      description: errorMessage(err),
    });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.feedAll() });
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.feed.counts() });
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.detail(paperId) });
  };

  const saveMut = useMutation({
    mutationFn: () => savePaper(paperId),
    onSuccess: () => {
      recordResearchMilestone('save');
      invalidate();
    },
    onError: toastError('save'),
  });

  const skipMut = useMutation({
    mutationFn: () => skipPaper(paperId),
    onSuccess: invalidate,
    onError: toastError('skip'),
  });

  const markReadingMut = useMutation({
    mutationFn: () => markReading(paperId),
    onSuccess: invalidate,
    onError: toastError('mark as reading'),
  });

  const markDoneMut = useMutation({
    mutationFn: () => markDone(paperId),
    onSuccess: invalidate,
    onError: toastError('mark as done'),
  });

  const trashMut = useMutation({
    mutationFn: () => trashPaper(paperId),
    onSuccess: invalidate,
    onError: toastError('trash'),
  });

  const restoreMut = useMutation({
    mutationFn: () => restorePaper(paperId),
    onSuccess: invalidate,
    onError: toastError('restore'),
  });

  const starMut = useMutation({
    mutationFn: () => (starred ? unstarPaper(paperId) : starPaper(paperId)),
    onSuccess: invalidate,
    onError: toastError(starred ? 'unstar' : 'star'),
  });

  const hardDeleteMut = useMutation({
    mutationFn: () => hardDeletePaper(paperId),
    onSuccess: invalidate,
    onError: toastError('delete'),
  });

  const handleTrash = () => {
    toast.warning('Move to Trash?', {
      action: {
        label: 'Confirm',
        onClick: () => trashMut.mutate(),
      },
    });
  };

  // State-contextual primary/secondary action buttons (mirrors PaperHeader)
  const renderActionButtons = () => {
    if (state === 'inbox') {
      return (
        <>
          <Button
            variant="default"
            size="sm"
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending}
            title="Save to reading list"
            aria-label="Save paper"
          >
            <Save className="mr-1 h-4 w-4" />
            Save
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => skipMut.mutate()}
            disabled={skipMut.isPending}
            title="Skip this paper"
            aria-label="Skip paper"
          >
            <SkipForward className="mr-1 h-4 w-4" />
            Skip
          </Button>
        </>
      );
    }

    if (state === 'to_read') {
      return (
        <Button
          variant="default"
          size="sm"
          onClick={() => markReadingMut.mutate()}
          disabled={markReadingMut.isPending}
          title="Start reading"
          aria-label="Mark as reading"
        >
          <BookOpen className="mr-1 h-4 w-4" />
          Start Reading
        </Button>
      );
    }

    if (state === 'reading') {
      return (
        <>
          <Button
            variant="default"
            size="sm"
            onClick={() => markDoneMut.mutate()}
            disabled={markDoneMut.isPending}
            title="Mark as done"
            aria-label="Mark as done"
          >
            <CheckCircle className="mr-1 h-4 w-4" />
            Mark Done
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending}
            title="Pause reading (back to reading list)"
            aria-label="Pause reading"
          >
            <Library className="mr-1 h-4 w-4" />
            Pause reading
          </Button>
        </>
      );
    }

    if (state === 'done') {
      return (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => markReadingMut.mutate()}
          disabled={markReadingMut.isPending}
          title="Resume reading"
          aria-label="Resume reading"
        >
          <RotateCcw className="mr-1 h-4 w-4" />
          Resume reading
        </Button>
      );
    }

    if (state === 'trash') {
      return (
        <>
          <Button
            variant="default"
            size="sm"
            onClick={() => restoreMut.mutate()}
            disabled={restoreMut.isPending}
            title="Restore from trash"
            aria-label="Restore paper"
          >
            <ArchiveRestore className="mr-1 h-4 w-4" />
            Restore
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            onClick={() => setHardDeleteOpen(true)}
            disabled={hardDeleteMut.isPending}
            title="Permanently delete"
            aria-label="Permanently delete paper"
          >
            <Trash className="mr-1 h-4 w-4" />
            Permanently delete
          </Button>
        </>
      );
    }

    return null;
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      {renderActionButtons()}

      {/* Star toggle — always shown except trash */}
      {state !== 'trash' && (
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0"
          onClick={() => starMut.mutate()}
          disabled={starMut.isPending}
          title={starred ? 'Starred — click to unstar' : 'Star this paper'}
          aria-label={starred ? 'Starred' : 'Star paper'}
        >
          {starred ? (
            <StarOff className="h-4 w-4 text-yellow-400" />
          ) : (
            <Star className="h-4 w-4 text-muted-foreground" />
          )}
        </Button>
      )}

      {/* Trash — shown on all non-trash states */}
      {state !== 'trash' && (
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0"
          onClick={handleTrash}
          disabled={trashMut.isPending}
          title="Move to Trash"
          aria-label="Trash paper"
        >
          <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
        </Button>
      )}

      {/* The buttons alone do not say what the reading states mean or what
          order they run in, so the sequence stays available as an info icon
          rather than another button competing for the toolbar. */}
      <InfoTooltip
        content="Where this paper sits in your reading workflow: Inbox (unsorted) → Reading List (saved to read) → Reading → Done (finished). Move papers to Trash to remove them."
        side="bottom"
      />

      <HardDeleteModal
        open={hardDeleteOpen}
        onOpenChange={setHardDeleteOpen}
        paperId={paperId}
        paperTitle={paperTitle}
      />
    </div>
  );
}

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import { hardDeletePaper } from '@/lib/api';
import { errorMessage } from '@/lib/errors';

// ---------------------------------------------------------------------------
// Per-row controlled mode (single paper, controlled open state)
// ---------------------------------------------------------------------------

interface HardDeleteModalSingleProps {
  /** Controlled open state — used by per-row X button in FeedPaperRow. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  paperId: number;
  paperTitle: string;
  onDeleted?: () => void;
  /** Must NOT provide bulk props in this mode. */
  count?: never;
  onConfirm?: never;
  trigger?: never;
}

// ---------------------------------------------------------------------------
// Bulk trigger mode (N papers, caller owns the mutation)
// ---------------------------------------------------------------------------

interface HardDeleteModalBulkProps {
  /** Number of papers to delete. */
  count: number;
  /** Called when the user confirms — caller fires the actual mutation. */
  onConfirm: () => void;
  /** The trigger element rendered inside AlertDialogTrigger. */
  trigger: React.ReactNode;
  /** Must NOT provide single-paper props in this mode. */
  open?: never;
  onOpenChange?: never;
  paperId?: never;
  paperTitle?: never;
  onDeleted?: never;
}

type HardDeleteModalProps = HardDeleteModalSingleProps | HardDeleteModalBulkProps;

export function HardDeleteModal(props: HardDeleteModalProps) {
  const isBulk = props.count !== undefined;

  if (isBulk) {
    return <HardDeleteModalBulk count={props.count} onConfirm={props.onConfirm} trigger={props.trigger} />;
  }

  return (
    <HardDeleteModalSingle
      open={props.open}
      onOpenChange={props.onOpenChange}
      paperId={props.paperId}
      paperTitle={props.paperTitle}
      onDeleted={props.onDeleted}
    />
  );
}

// ---------------------------------------------------------------------------
// Internal: single-paper controlled dialog
// ---------------------------------------------------------------------------

function HardDeleteModalSingle({
  open,
  onOpenChange,
  paperId,
  paperTitle,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  paperId: number;
  paperTitle: string;
  onDeleted?: () => void;
}) {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => hardDeletePaper(paperId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.feedAll() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.feed.counts() });
      toast.success('Paper removed from your library');
      onOpenChange(false);
      onDeleted?.();
    },
    onError: (err) =>
      toast.error('Failed to delete paper', {
        description: errorMessage(err),
      }),
  });

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Permanently delete this paper?</AlertDialogTitle>
          <AlertDialogDescription>
            &quot;{paperTitle}&quot; will be removed from your library along with your
            summaries, notes, highlights, and other private activity. Shared search content may
            remain available for reuse, so this does not remove the paper system-wide. This cannot
            be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
            autoFocus
          >
            {mutation.isPending ? 'Deleting…' : 'Delete'}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// ---------------------------------------------------------------------------
// Internal: bulk N-paper trigger dialog (caller owns mutation)
// ---------------------------------------------------------------------------

function HardDeleteModalBulk({
  count,
  onConfirm,
  trigger,
}: {
  count: number;
  onConfirm: () => void;
  trigger: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);

  const title = count === 1
    ? 'Permanently delete this paper?'
    : `Permanently delete ${count} papers?`;

  const body = count === 1
    ? 'This paper will be removed from your library along with your private notes, summaries, and activity. Shared search content may remain available for reuse, so this does not remove the paper system-wide. This cannot be undone.'
    : `${count} papers will be removed from your library along with your private notes, summaries, and activity. Shared search content may remain available for reuse, so this does not remove these papers system-wide. This cannot be undone.`;

  function handleConfirm() {
    setOpen(false);
    onConfirm();
  }

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{body}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={handleConfirm} autoFocus>
            Permanently delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

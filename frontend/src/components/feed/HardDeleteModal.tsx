import { useMutation, useQueryClient } from '@tanstack/react-query';
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
} from '@/components/ui/alert-dialog';
import { hardDeletePaper } from '@/lib/api';

interface HardDeleteModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  paperId: number;
  paperTitle: string;
  onDeleted?: () => void;
}

export function HardDeleteModal({ open, onOpenChange, paperId, paperTitle, onDeleted }: HardDeleteModalProps) {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => hardDeletePaper(paperId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
      queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
      toast.success('Paper deleted');
      onOpenChange(false);
      onDeleted?.();
    },
    onError: (err) =>
      toast.error('Failed to delete paper', {
        description: err instanceof Error ? err.message : 'Unknown error',
      }),
  });

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Permanently delete this paper?</AlertDialogTitle>
          <AlertDialogDescription>
            &quot;{paperTitle}&quot; will be removed from JARVIS, including all chunks,
            summaries, notes, and pulse history. This cannot be undone.
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

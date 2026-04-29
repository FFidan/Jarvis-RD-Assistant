import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { hardDeletePaper } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';

interface HardDeleteModalProps {
  open: boolean;
  paperId: number | null;
  paperTitle: string;
  onClose: () => void;
}

export function HardDeleteModal({ open, paperId, paperTitle, onClose }: HardDeleteModalProps) {
  const [confirmText, setConfirmText] = useState('');
  const [alsoZotero, setAlsoZotero] = useState(false);
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => hardDeletePaper(paperId!, { confirm_title: confirmText, also_zotero: alsoZotero }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
      queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
      setConfirmText('');
      setAlsoZotero(false);
      onClose();
    },
  });

  const matches = confirmText.trim() === paperTitle.trim();

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete forever</DialogTitle>
          <DialogDescription>
            This permanently deletes the paper, its summary, chunks, and vector embeddings.
            Citation graph edges and learning cards are preserved (paper_id becomes null on those rows).
            <br /><br />
            Type the paper&apos;s title exactly to confirm:
            <br />
            <strong>{paperTitle}</strong>
          </DialogDescription>
        </DialogHeader>
        <Input
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          placeholder="Paper title"
        />
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="also-zotero"
            checked={alsoZotero}
            onChange={(e) => setAlsoZotero(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300 text-primary focus:ring-primary"
          />
          <label htmlFor="also-zotero" className="text-sm">
            Also remove from Zotero{' '}
            <span className="text-muted-foreground">(coming soon)</span>
          </label>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!matches || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? 'Deleting...' : 'Delete forever'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

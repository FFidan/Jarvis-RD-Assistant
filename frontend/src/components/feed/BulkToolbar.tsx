import { useBulkSelection } from '@/stores/bulk-selection-store';
import { bulkAction } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
import type { BulkAction, SurfaceView } from '@/types';

interface BulkToolbarProps {
  surface: SurfaceView;
}

const SURFACE_ACTIONS: Record<SurfaceView, BulkAction[]> = {
  inbox: ['save', 'star', 'mark_read', 'dismiss'],
  library: ['star', 'archive', 'mark_read', 'dismiss', 'unsave'],
  starred: ['unstar', 'archive', 'mark_read', 'dismiss'],
  archived: ['unarchive', 'mark_read', 'dismiss'],
  reading: ['mark_read', 'star', 'archive', 'dismiss'],
  // trash: per-row Restore + HardDelete; no bulk actions
  trash: [],
  search: [],
  ask: [],
};

const ACTION_LABELS: Record<BulkAction, string> = {
  save: 'Save',
  unsave: 'Unsave',
  dismiss: 'Dismiss',
  archive: 'Archive',
  unarchive: 'Unarchive',
  mark_read: 'Mark Read',
  star: 'Star',
  unstar: 'Unstar',
};

export function BulkToolbar({ surface }: BulkToolbarProps) {
  const { selectedIds, clear } = useBulkSelection();
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: ({ action }: { action: BulkAction }) =>
      bulkAction({ paper_ids: Array.from(selectedIds), action }),
    onSuccess: (data, vars) => {
      const ok = data.succeeded.length;
      const fail = data.failed.length;
      toast.success(`${ACTION_LABELS[vars.action]}: ${ok} succeeded${fail > 0 ? `, ${fail} failed` : ''}`);
      void queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
      void queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
      clear();
    },
    onError: (err) => {
      toast.error(`Bulk action failed: ${err instanceof Error ? err.message : 'Unknown error'}`);
    },
  });

  const actions = SURFACE_ACTIONS[surface];

  if (selectedIds.size === 0 || actions.length === 0) return null;

  return (
    <div className="sticky top-0 z-10 flex items-center gap-2 border-b bg-background px-4 py-2">
      <span className="text-sm font-medium">
        {selectedIds.size} selected
      </span>
      {actions.map((action) => (
        <Button
          key={action}
          size="sm"
          variant="outline"
          onClick={() => mutation.mutate({ action })}
          disabled={mutation.isPending}
        >
          {ACTION_LABELS[action]}
        </Button>
      ))}
      <Button size="sm" variant="ghost" onClick={clear}>
        Clear
      </Button>
    </div>
  );
}

import { useBulkSelection } from '@/stores/bulk-selection-store';
import { bulkAction } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
import {
  Save,
  SkipForward,
  Trash2,
  BookOpen,
  CheckCircle,
  ArchiveRestore,
  Star,
  StarOff,
  ThumbsUp,
  ThumbsDown,
} from 'lucide-react';
import type { BulkAction, SurfaceView } from '@/types';

interface BulkToolbarProps {
  surface: SurfaceView;
}

const SURFACE_ACTIONS: Record<SurfaceView, BulkAction[]> = {
  inbox:   ['save', 'skip', 'trash', 'star', 'unstar', 'feedback_positive', 'feedback_negative'],
  library: ['mark_reading', 'mark_done', 'trash', 'star', 'unstar'],
  trash:   ['restore'],
  search:  [],
  ask:     [],
};

interface ActionConfig {
  label: string;
  icon: React.ReactNode;
}

const ACTION_CONFIG: Record<BulkAction, ActionConfig> = {
  save:              { label: 'Save to Library', icon: <Save className="h-3.5 w-3.5" /> },
  skip:              { label: 'Skip',            icon: <SkipForward className="h-3.5 w-3.5" /> },
  trash:             { label: 'Move to Trash',   icon: <Trash2 className="h-3.5 w-3.5" /> },
  mark_reading:      { label: 'Mark Reading',    icon: <BookOpen className="h-3.5 w-3.5" /> },
  mark_done:         { label: 'Mark Done',       icon: <CheckCircle className="h-3.5 w-3.5" /> },
  restore:           { label: 'Restore',         icon: <ArchiveRestore className="h-3.5 w-3.5" /> },
  star:              { label: 'Star',            icon: <Star className="h-3.5 w-3.5" /> },
  unstar:            { label: 'Unstar',          icon: <StarOff className="h-3.5 w-3.5" /> },
  feedback_positive: { label: '👍',              icon: <ThumbsUp className="h-3.5 w-3.5" /> },
  feedback_negative: { label: '👎',              icon: <ThumbsDown className="h-3.5 w-3.5" /> },
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
      const label = ACTION_CONFIG[vars.action].label;
      toast.success(`${label}: ${ok} succeeded${fail > 0 ? `, ${fail} failed` : ''}`);
      void queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
      void queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
      clear();
    },
    onError: (err) => {
      toast.error('Bulk action failed', { description: err instanceof Error ? err.message : 'Unknown error' });
    },
  });

  const actions = SURFACE_ACTIONS[surface];

  if (selectedIds.size === 0 || actions.length === 0) return null;

  return (
    <div className="sticky top-0 z-10 flex items-center gap-2 border-b bg-background px-4 py-2">
      <span aria-live="polite" className="text-sm font-medium">
        {selectedIds.size} selected
      </span>
      {actions.map((action) => {
        const cfg = ACTION_CONFIG[action];
        return (
          <Button
            key={action}
            size="sm"
            variant="outline"
            onClick={() => mutation.mutate({ action })}
            disabled={mutation.isPending}
            className="gap-1.5"
          >
            {cfg.icon}
            {cfg.label}
          </Button>
        );
      })}
      <Button size="sm" variant="ghost" onClick={clear}>
        Clear
      </Button>
    </div>
  );
}

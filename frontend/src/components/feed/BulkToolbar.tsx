import { useBulkSelection } from '@/stores/bulk-selection-store';
import { bulkAction } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
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
  X,
} from 'lucide-react';
import type { BulkAction, SurfaceView } from '@/types';
import { HardDeleteModal } from './HardDeleteModal';

interface BulkToolbarProps {
  surface: SurfaceView;
  /** IDs of papers currently rendered on the page — used for Select All. */
  papersOnPage: number[];
}

const SURFACE_ACTIONS: Record<SurfaceView, BulkAction[]> = {
  inbox:   ['save', 'skip', 'trash', 'star', 'unstar', 'feedback_positive', 'feedback_negative'],
  library: ['mark_reading', 'mark_done', 'trash', 'star', 'unstar'],
  trash:   ['restore', 'hard_delete'],
  search:  [],
  ask:     [],
};

interface ActionConfig {
  label: string;
  tooltip: string;
  icon: React.ReactNode;
}

const ACTION_CONFIG: Record<BulkAction, ActionConfig> = {
  save:              { label: 'Save to Reading List',  tooltip: 'Save to Reading List',                                         icon: <Save className="h-3.5 w-3.5" /> },
  skip:              { label: 'Skip',                  tooltip: 'Skip: move to Done without saving to Reading List',             icon: <SkipForward className="h-3.5 w-3.5" /> },
  trash:             { label: 'Move to Trash',         tooltip: 'Move to Trash',                                                icon: <Trash2 className="h-3.5 w-3.5" /> },
  mark_reading:      { label: 'Mark Reading',          tooltip: 'Mark Reading: move to Reading state — currently active',       icon: <BookOpen className="h-3.5 w-3.5" /> },
  mark_done:         { label: 'Mark Done',             tooltip: 'Mark Done: mark as finished and move to Done',                 icon: <CheckCircle className="h-3.5 w-3.5" /> },
  restore:           { label: 'Restore',               tooltip: 'Restore',                                                      icon: <ArchiveRestore className="h-3.5 w-3.5" /> },
  star:              { label: 'Star',                  tooltip: 'Star',                                                         icon: <Star className="h-3.5 w-3.5" /> },
  unstar:            { label: 'Unstar',                tooltip: 'Unstar',                                                       icon: <StarOff className="h-3.5 w-3.5" /> },
  feedback_positive: { label: 'More like this',        tooltip: 'More like this',                                               icon: <ThumbsUp className="h-3.5 w-3.5" /> },
  feedback_negative: { label: 'Less like this',        tooltip: 'Less like this',                                               icon: <ThumbsDown className="h-3.5 w-3.5" /> },
  hard_delete:       { label: 'Delete forever',        tooltip: 'Permanently delete the selected papers (cannot be undone)',     icon: <X className="h-3.5 w-3.5" /> },
};

export function BulkToolbar({ surface, papersOnPage }: BulkToolbarProps) {
  const { selectedIds, clear, selectMany } = useBulkSelection();
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

  // Don't render the toolbar on surfaces with no actions or when the page is empty
  if (actions.length === 0 || papersOnPage.length === 0) return null;

  const allChecked = papersOnPage.length > 0 && papersOnPage.every((id) => selectedIds.has(id));
  const someChecked = papersOnPage.some((id) => selectedIds.has(id));
  const indeterminate = someChecked && !allChecked;

  function handleSelectAll() {
    if (allChecked || indeterminate) {
      clear();
    } else {
      selectMany(papersOnPage);
    }
  }

  return (
    <TooltipProvider delayDuration={400}>
      <div className="sticky top-2 z-10 flex items-center gap-2 rounded-lg border border-hair bg-paper/95 backdrop-blur-sm shadow-sm mx-4 px-3 py-1.5">
        {/* Select All checkbox */}
        <Tooltip>
          <TooltipTrigger asChild>
            <label className="flex cursor-pointer items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={allChecked}
                ref={(el) => {
                  if (el) el.indeterminate = indeterminate;
                }}
                onChange={handleSelectAll}
                aria-label="Select all on this page"
                className="h-4 w-4 cursor-pointer accent-primary"
              />
              <span aria-live="polite" className="font-medium">
                {selectedIds.size > 0 ? `${selectedIds.size} selected` : 'Select all'}
              </span>
            </label>
          </TooltipTrigger>
          <TooltipContent>Select all on this page</TooltipContent>
        </Tooltip>

        {selectedIds.size > 0 && (
          <>
            {actions.map((action) => {
              const cfg = ACTION_CONFIG[action];
              if (action === 'hard_delete') {
                return (
                  <HardDeleteModal
                    key={action}
                    count={selectedIds.size}
                    onConfirm={() => mutation.mutate({ action })}
                    trigger={
                      <Button
                        size="sm"
                        variant="destructive"
                        disabled={mutation.isPending}
                        className="gap-1.5"
                      >
                        {cfg.icon}
                        {cfg.label}
                      </Button>
                    }
                  />
                );
              }
              return (
                <Tooltip key={action}>
                  <TooltipTrigger asChild>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => mutation.mutate({ action })}
                      disabled={mutation.isPending}
                      className="gap-1.5"
                    >
                      {cfg.icon}
                      {cfg.label}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>{cfg.tooltip}</TooltipContent>
                </Tooltip>
              );
            })}
            <Button size="sm" variant="ghost" onClick={clear}>
              Clear
            </Button>
          </>
        )}
      </div>
    </TooltipProvider>
  );
}

/**
 * FeedPaperRowActions — the action bar of a paper row.
 *
 * One primary action for the paper's lifecycle state, plus View, plus an
 * overflow menu holding the remaining lifecycle actions and citation export.
 * A list page shows dozens of rows at once, so a full six-control bar per row
 * buries the one action that actually moves the paper forward; the complete
 * action set stays on Paper Detail.
 */

import {
  ArchiveRestore,
  BookOpen,
  Check,
  MoreHorizontal,
  Quote,
  RotateCcw,
  Save,
  SkipForward,
  Star,
  StarOff,
  Trash2,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { CitationMenuItems } from '@/components/citation/CitationMenu';
import type { LifecycleState } from '@/types';

interface FeedPaperRowActionsProps {
  paperId: number;
  title: string;
  state: LifecycleState;
  isStarred: boolean;
  viewLabel: string;
  onSave?: (id: number) => void;
  onSkip?: (id: number) => void;
  onMarkReading?: (id: number) => void;
  onMarkDone?: (id: number) => void;
  onSetAside?: (id: number) => void;
  onReopen?: (id: number) => void;
  onTrash?: (id: number) => void;
  onStar?: (id: number) => void;
  onUnstar?: (id: number) => void;
  onRestore?: (id: number) => void;
  onHardDelete?: (id: number) => void;
  onView?: (id: number) => void;
}

interface ActionSpec {
  label: string;
  icon: React.ReactNode;
  handler: ((id: number) => void) | undefined;
  destructive?: boolean;
}

export function FeedPaperRowActions({
  paperId,
  title,
  state,
  isStarred,
  viewLabel,
  onSave,
  onSkip,
  onMarkReading,
  onMarkDone,
  onSetAside,
  onReopen,
  onTrash,
  onStar,
  onUnstar,
  onRestore,
  onHardDelete,
  onView,
}: FeedPaperRowActionsProps) {
  const starAction: ActionSpec = isStarred
    ? { label: 'Unstar', icon: <StarOff className="mr-2 h-3.5 w-3.5" />, handler: onUnstar }
    : { label: 'Star', icon: <Star className="mr-2 h-3.5 w-3.5" />, handler: onStar };
  const trashAction: ActionSpec = {
    label: 'Move to Trash',
    icon: <Trash2 className="mr-2 h-3.5 w-3.5" />,
    handler: onTrash,
    destructive: true,
  };

  // One primary action per lifecycle state; everything else is overflow.
  let primary: ActionSpec | null;
  let overflow: ActionSpec[];
  switch (state) {
    case 'inbox':
      primary = { label: 'Save', icon: <Save className="mr-1 h-3 w-3" />, handler: onSave };
      overflow = [
        { label: 'Skip', icon: <SkipForward className="mr-2 h-3.5 w-3.5" />, handler: onSkip },
        starAction,
        trashAction,
      ];
      break;
    case 'to_read':
      primary = {
        label: 'Start reading',
        icon: <BookOpen className="mr-1 h-3 w-3" />,
        handler: onMarkReading,
      };
      overflow = [
        { label: 'Mark Done', icon: <Check className="mr-2 h-3.5 w-3.5" />, handler: onMarkDone },
        starAction,
        trashAction,
      ];
      break;
    case 'reading':
      primary = { label: 'Mark Done', icon: <Check className="mr-1 h-3 w-3" />, handler: onMarkDone };
      overflow = [
        {
          label: 'Pause reading',
          icon: <RotateCcw className="mr-2 h-3.5 w-3.5" />,
          handler: onSetAside,
        },
        starAction,
        trashAction,
      ];
      break;
    case 'done':
      primary = { label: 'Reopen', icon: <RotateCcw className="mr-1 h-3 w-3" />, handler: onReopen };
      overflow = [starAction, trashAction];
      break;
    case 'trash':
      primary = {
        label: 'Restore',
        icon: <ArchiveRestore className="mr-1 h-3 w-3" />,
        handler: onRestore,
      };
      overflow = [
        {
          label: 'Permanently delete',
          icon: <X className="mr-2 h-3.5 w-3.5" />,
          handler: onHardDelete,
          destructive: true,
        },
      ];
      break;
    default:
      primary = null;
      overflow = [starAction, trashAction];
  }

  const availableOverflow = overflow.filter((a) => a.handler);

  return (
    <div className="mt-auto flex items-center gap-1">
      {primary?.handler && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => primary.handler?.(paperId)}
          aria-label={`${primary.label} ${title}`}
        >
          {primary.icon}
          {primary.label}
        </Button>
      )}
      {onView && (
        <Button
          variant="default"
          size="sm"
          onClick={() => onView(paperId)}
          aria-label={`View ${title} details`}
        >
          {viewLabel}
        </Button>
      )}
      {/* Always present: citing this paper needs no caller-supplied handler,
          so the menu has something to offer even on a read-only row. */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-9 w-9"
            aria-label={`More actions for ${title}`}
          >
            <MoreHorizontal className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {availableOverflow.map((action) => (
            <DropdownMenuItem
              key={action.label}
              onClick={() => action.handler?.(paperId)}
              className={action.destructive ? 'text-destructive focus:text-destructive' : undefined}
            >
              {action.icon}
              {action.label}
            </DropdownMenuItem>
          ))}
          {/* Citing one paper belongs here rather than on the row: it is a
              real per-paper action, but not one worth a permanent control. */}
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <Quote className="mr-2 h-3.5 w-3.5" />
              Cite
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              <CitationMenuItems paperIds={[paperId]} />
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

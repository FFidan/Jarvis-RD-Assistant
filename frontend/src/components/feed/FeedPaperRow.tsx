import {
  Archive,
  ArchiveRestore,
  CheckCircle,
  Eye,
  Loader2,
  Save,
  Star,
  StarOff,
  Trash2,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { formatAuthors, formatDate } from '@/lib/utils';
import { type FeedPaper, priorityLevel } from '@/types';

interface FeedPaperRowProps {
  paper: FeedPaper;
  // Cross-paper RAG seed selection (separate from bulk selection)
  seedChecked?: boolean;
  onSeedChange?: (paperId: number) => void;
  // Existing action callbacks
  onMarkRead?: (paperId: number) => void;
  markReadPending?: boolean;
  onArchive?: (paperId: number) => void;
  archivePending?: boolean;
  onView?: (paperId: number) => void;
  viewLabel?: string;
  // New surface-aware action callbacks (all optional)
  onSave?: (paperId: number) => void;
  savePending?: boolean;
  onSaveAndStar?: (paperId: number) => void;
  saveAndStarPending?: boolean;
  onDismiss?: (paperId: number) => void;
  dismissPending?: boolean;
  onRestore?: (paperId: number) => void;
  restorePending?: boolean;
  onHardDelete?: (paperId: number) => void;
  hardDeletePending?: boolean;
  onStar?: (paperId: number) => void;
  onUnstar?: (paperId: number) => void;
  onUnarchive?: (paperId: number) => void;
  // Bulk selection
  bulkSelected?: boolean;
  onBulkToggle?: (paperId: number) => void;
}

export function FeedPaperRow({
  paper,
  seedChecked,
  onSeedChange,
  onMarkRead,
  markReadPending = false,
  onArchive,
  archivePending = false,
  onView,
  viewLabel = 'View',
  onSave,
  savePending = false,
  onSaveAndStar,
  saveAndStarPending = false,
  onDismiss,
  dismissPending = false,
  onRestore,
  restorePending = false,
  onHardDelete,
  hardDeletePending = false,
  onStar,
  onUnstar,
  onUnarchive,
  bulkSelected,
  onBulkToggle,
}: FeedPaperRowProps) {
  const userState = paper.user_state;
  const isSaved = userState?.saved ?? false;
  const isDismissed = userState?.dismissed ?? false;
  const isStarred = userState?.starred ?? paper.starred ?? false;
  const isArchived = userState?.archived ?? paper.archived ?? false;
  const status = userState?.status ?? 'new';

  const isNew = status === 'new' && !isDismissed;

  return (
    <div className="rounded-lg border p-4">
      <div className="flex gap-3">
        {/* Bulk selection checkbox */}
        {onBulkToggle && (
          <div className="flex items-start pt-1">
            <input
              type="checkbox"
              checked={!!bulkSelected}
              onChange={() => onBulkToggle(paper.id)}
              className="h-4 w-4 rounded border-gray-300"
              aria-label={`Select ${paper.title} for bulk action`}
            />
          </div>
        )}

        {/* Cross-paper RAG seed selection */}
        {onSeedChange && (
          <div className="flex items-start pt-1">
            <input
              type="checkbox"
              checked={Boolean(seedChecked)}
              onChange={() => onSeedChange(paper.id)}
              className="h-4 w-4 rounded border-gray-300"
              aria-label={`Select ${paper.title} as seed`}
              title="Select as seed for discovery"
            />
          </div>
        )}

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-lg font-semibold leading-tight">{paper.title}</h3>
            {isNew && (
              <Badge variant="default" className="text-xs">NEW</Badge>
            )}
            {isStarred && (
              <span className="text-amber-400" title="Starred">★</span>
            )}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {formatAuthors(paper.authors)}
          </p>
          {paper.tldr && (
            <p className="mt-2 text-sm italic">{paper.tldr}</p>
          )}
          {!paper.tldr && paper.summary_brief && (
            <p className="mt-2 line-clamp-3 text-sm">{paper.summary_brief}</p>
          )}
          {paper.note_match_count ? (
            <p className="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-900">
              Zotero note match{paper.note_match_count > 1 ? `es (${paper.note_match_count})` : ''}
              {paper.note_snippet ? `: ${paper.note_snippet}` : ''}
            </p>
          ) : null}
        </div>

        <div className="flex shrink-0 flex-col items-end gap-1">
          <Badge variant="outline">{paper.source_type.toUpperCase()}</Badge>
          <Badge variant="secondary">{status.toUpperCase()}</Badge>
          {paper.confidence && (
            <Badge variant={paper.confidence === 'HIGH' ? 'default' : 'secondary'}>
              {paper.confidence}
            </Badge>
          )}
          <PriorityBadge level={priorityLevel(paper.priority_score)} />
          <div className="flex gap-1">
            {paper.pdf_downloaded && <Badge variant="outline" className="px-1.5 py-0 text-xs">PDF</Badge>}
            {paper.has_chunks && <Badge variant="outline" className="px-1.5 py-0 text-xs">Chunked</Badge>}
            {paper.has_summary && <Badge variant="outline" className="px-1.5 py-0 text-xs">Summary</Badge>}
          </div>
          {paper.recommendation_score != null && paper.recommendation_reason && (
            <Badge variant="outline" className="border-blue-300 bg-blue-50 text-xs text-blue-600">
              ★ {paper.recommendation_reason}
            </Badge>
          )}
          <span
            className="text-xs text-muted-foreground"
            title={
              paper.discovered_at && paper.published_date
                ? `Published: ${formatDate(paper.published_date)}`
                : undefined
            }
          >
            {formatDate(paper.discovered_at || paper.published_date || paper.created_at)}
          </span>

          {/* Surface-aware action button bar — only renders buttons whose callback is provided */}
          <div className="mt-auto flex flex-wrap gap-1">
            {/* Save (inbox) */}
            {onSave && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => onSave(paper.id)}
                disabled={savePending || isSaved}
                aria-label={`Save ${paper.title}`}
              >
                {savePending ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                ) : (
                  <Save className="mr-1 h-3 w-3" />
                )}
                Save
              </Button>
            )}

            {/* Save & Star (inbox) */}
            {onSaveAndStar && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => onSaveAndStar(paper.id)}
                disabled={saveAndStarPending}
                aria-label={`Save and star ${paper.title}`}
              >
                {saveAndStarPending ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                ) : (
                  <Star className="mr-1 h-3 w-3" />
                )}
                Save &amp; Star
              </Button>
            )}

            {/* Star / Unstar (library, starred) */}
            {onStar && !isStarred && (
              <Button
                variant="outline"
                size="icon"
                onClick={() => onStar(paper.id)}
                aria-label={`Star ${paper.title}`}
                title="Star"
                className="h-9 w-9"
              >
                <Star className="h-4 w-4" />
              </Button>
            )}
            {onUnstar && isStarred && (
              <Button
                variant="outline"
                size="icon"
                onClick={() => onUnstar(paper.id)}
                aria-label={`Unstar ${paper.title}`}
                title="Unstar"
                className="h-9 w-9"
              >
                <StarOff className="h-4 w-4" />
              </Button>
            )}

            {/* Mark Read */}
            {onMarkRead && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => onMarkRead(paper.id)}
                disabled={markReadPending}
                aria-label={`Mark ${paper.title} as read`}
              >
                {markReadPending ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                ) : (
                  <CheckCircle className="mr-1 h-3 w-3" />
                )}
                Mark Read
              </Button>
            )}

            {/* Archive (library) */}
            {onArchive && (
              <Button
                variant="outline"
                size="icon"
                onClick={() => onArchive(paper.id)}
                disabled={archivePending}
                aria-label={`Archive ${paper.title}`}
                title="Archive"
                className="h-9 w-9"
              >
                {archivePending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Archive className="h-4 w-4" />
                )}
              </Button>
            )}

            {/* Unarchive (archived surface) */}
            {onUnarchive && (
              <Button
                variant="outline"
                size="icon"
                onClick={() => onUnarchive(paper.id)}
                aria-label={`Unarchive ${paper.title}`}
                title="Unarchive"
                className="h-9 w-9"
              >
                <ArchiveRestore className="h-4 w-4" />
              </Button>
            )}

            {/* Dismiss */}
            {onDismiss && (
              <Button
                variant="outline"
                size="icon"
                onClick={() => onDismiss(paper.id)}
                disabled={dismissPending || isDismissed}
                aria-label={`Dismiss ${paper.title}`}
                title="Dismiss"
                className="h-9 w-9"
              >
                {dismissPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <X className="h-4 w-4" />
                )}
              </Button>
            )}

            {/* Restore (trash) */}
            {onRestore && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => onRestore(paper.id)}
                disabled={restorePending}
                aria-label={`Restore ${paper.title}`}
              >
                {restorePending ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                ) : (
                  <ArchiveRestore className="mr-1 h-3 w-3" />
                )}
                Restore
              </Button>
            )}

            {/* Hard Delete (trash) */}
            {onHardDelete && (
              <Button
                variant="destructive"
                size="icon"
                onClick={() => onHardDelete(paper.id)}
                disabled={hardDeletePending}
                aria-label={`Permanently delete ${paper.title}`}
                title="Permanently delete"
                className="h-9 w-9"
              >
                {hardDeletePending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="h-4 w-4" />
                )}
              </Button>
            )}

            {/* View */}
            {onView && (
              <Button
                variant="default"
                size="sm"
                onClick={() => onView(paper.id)}
                aria-label={`View ${paper.title} details`}
              >
                <Eye className="mr-1 h-3 w-3" />
                {viewLabel}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

import {
  ArchiveRestore,
  BookOpen,
  CheckCircle,
  Library,
  RotateCcw,
  Save,
  SkipForward,
  Star,
  StarOff,
  Trash2,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { FeedbackButtons } from '@/components/shared/FeedbackButtons';
import { formatAuthors, formatDate } from '@/lib/utils';
import { type FeedPaper, type SurfaceView, priorityLevel } from '@/types';

export interface FeedPaperRowProps {
  paper: FeedPaper;
  surface?: SurfaceView;
  // Bulk selection
  isSelected?: boolean;
  onToggleSelect?: (paperId: number) => void;
  /** @deprecated use isSelected/onToggleSelect */
  bulkSelected?: boolean;
  /** @deprecated use isSelected/onToggleSelect */
  onBulkToggle?: (paperId: number) => void;
  // Cross-paper RAG seed selection
  seedChecked?: boolean;
  onSeedChange?: (paperId: number) => void;
  // Lifecycle callbacks
  onSave?: (id: number) => void;
  onSkip?: (id: number) => void;
  onMarkReading?: (id: number) => void;
  onMarkDone?: (id: number) => void;
  /** Reading → to_read (uses /save endpoint which sets state='to_read' unconditionally). */
  onSetAside?: (id: number) => void;
  /** Done → reading */
  onReopen?: (id: number) => void;
  onTrash?: (id: number) => void;
  onStar?: (id: number) => void;
  onUnstar?: (id: number) => void;
  onRestore?: (id: number) => void;
  onHardDelete?: (id: number) => void;
  onView?: (paperId: number) => void;
  viewLabel?: string;
}

export function FeedPaperRow({
  paper,
  surface: _surface,
  isSelected,
  onToggleSelect,
  bulkSelected,
  onBulkToggle,
  seedChecked,
  onSeedChange,
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
  viewLabel = 'View',
}: FeedPaperRowProps) {
  const state = paper.state ?? 'inbox';
  const isStarred = paper.starred ?? false;

  // Normalise bulk selection props (support both old and new API)
  const effectiveSelected = isSelected ?? bulkSelected ?? false;
  const effectiveToggle = onToggleSelect ?? onBulkToggle;

  return (
    <div className="rounded-lg border p-4">
      <div className="flex gap-3">
        {/* Bulk selection checkbox */}
        {effectiveToggle && (
          <div className="flex items-start pt-1">
            <input
              type="checkbox"
              checked={effectiveSelected}
              onChange={() => effectiveToggle(paper.id)}
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
            {state === 'inbox' && (
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
          {/* Spec §5.5: Pulse-origin papers also appear in Inbox; this badge makes the
              overlap legible without separating the data models. */}
          {(paper.discovery_origin === 'pulse' || paper.discovery_origin === 'recommender') && (
            <Badge
              variant="secondary"
              className="bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200"
              title={
                paper.discovery_origin === 'pulse'
                  ? "Also in today's Pulse Deck"
                  : 'Surfaced by the recommender'
              }
            >
              ✦ {paper.discovery_origin === 'pulse' ? 'Pulse' : 'Recommended'}
            </Badge>
          )}
          <Badge variant="secondary">{state.toUpperCase()}</Badge>
          {paper.confidence && (
            <Badge variant={paper.confidence === 'HIGH' ? 'default' : 'secondary'}>
              {paper.confidence}
            </Badge>
          )}
          <PriorityBadge level={priorityLevel(paper.priority_score ?? null)} />
          <div className="flex gap-1">
            {/* FeedPaper has no pdf_downloaded/has_chunks/has_summary at top-level — they come from LifecyclePaperResponse extensions */}
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
              paper.created_at
                ? `Published: ${formatDate(paper.published_date ?? paper.created_at)}`
                : undefined
            }
          >
            {formatDate(paper.created_at)}
          </span>

          {/* State-switch action button bar */}
          <div className="mt-auto flex flex-wrap gap-1">
            {state === 'inbox' && (
              <>
                {onSave && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onSave(paper.id)}
                    aria-label={`Save ${paper.title}`}
                  >
                    <Save className="mr-1 h-3 w-3" />
                    Save
                  </Button>
                )}
                {onSkip && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onSkip(paper.id)}
                    aria-label={`Skip ${paper.title}`}
                  >
                    <SkipForward className="mr-1 h-3 w-3" />
                    Skip
                  </Button>
                )}
                {onTrash && (
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => onTrash(paper.id)}
                    aria-label={`Trash ${paper.title}`}
                    title="Move to Trash"
                    className="h-9 w-9"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                )}
                {isStarred
                  ? onUnstar && (
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
                  )
                  : onStar && (
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
                  )
                }
              </>
            )}

            {state === 'to_read' && (
              <>
                {onMarkReading && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onMarkReading(paper.id)}
                    aria-label={`Mark ${paper.title} as reading`}
                  >
                    <BookOpen className="mr-1 h-3 w-3" />
                    Mark Reading
                  </Button>
                )}
                {onMarkDone && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onMarkDone(paper.id)}
                    aria-label={`Mark ${paper.title} as done`}
                  >
                    <CheckCircle className="mr-1 h-3 w-3" />
                    Mark Done
                  </Button>
                )}
                {onTrash && (
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => onTrash(paper.id)}
                    aria-label={`Trash ${paper.title}`}
                    title="Move to Trash"
                    className="h-9 w-9"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                )}
                {isStarred
                  ? onUnstar && (
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
                  )
                  : onStar && (
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
                  )
                }
              </>
            )}

            {state === 'reading' && (
              <>
                {onSetAside && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onSetAside(paper.id)}
                    aria-label={`Set aside ${paper.title}`}
                  >
                    <Library className="mr-1 h-3 w-3" />
                    Set Aside
                  </Button>
                )}
                {onMarkDone && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onMarkDone(paper.id)}
                    aria-label={`Mark ${paper.title} as done`}
                  >
                    <CheckCircle className="mr-1 h-3 w-3" />
                    Mark Done
                  </Button>
                )}
                {onTrash && (
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => onTrash(paper.id)}
                    aria-label={`Trash ${paper.title}`}
                    title="Move to Trash"
                    className="h-9 w-9"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                )}
                {isStarred
                  ? onUnstar && (
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
                  )
                  : onStar && (
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
                  )
                }
              </>
            )}

            {state === 'done' && (
              <>
                {onReopen && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onReopen(paper.id)}
                    aria-label={`Re-open ${paper.title}`}
                  >
                    <RotateCcw className="mr-1 h-3 w-3" />
                    Re-open
                  </Button>
                )}
                {onTrash && (
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => onTrash(paper.id)}
                    aria-label={`Trash ${paper.title}`}
                    title="Move to Trash"
                    className="h-9 w-9"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                )}
                {isStarred
                  ? onUnstar && (
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
                  )
                  : onStar && (
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
                  )
                }
              </>
            )}

            {state === 'trash' && (
              <>
                {onRestore && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onRestore(paper.id)}
                    aria-label={`Restore ${paper.title}`}
                  >
                    <ArchiveRestore className="mr-1 h-3 w-3" />
                    Restore
                  </Button>
                )}
                {onHardDelete && (
                  <Button
                    variant="destructive"
                    size="icon"
                    onClick={() => onHardDelete(paper.id)}
                    aria-label={`Permanently delete ${paper.title}`}
                    title="Permanently delete"
                    className="h-9 w-9"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                )}
              </>
            )}

            {/* View */}
            {onView && (
              <Button
                variant="default"
                size="sm"
                onClick={() => onView(paper.id)}
                aria-label={`View ${paper.title} details`}
              >
                {viewLabel}
              </Button>
            )}
          </div>

          {/* Feedback buttons — hidden for trash surface and user-initiated papers */}
          {state !== 'trash' && (
            <FeedbackButtons
              paperId={paper.id}
              discoveryOrigin={paper.discovery_origin}
              source="feed_thumbs"
              recentFeedback={paper.recent_feedback ?? null}
              size="sm"
            />
          )}
        </div>
      </div>
    </div>
  );
}

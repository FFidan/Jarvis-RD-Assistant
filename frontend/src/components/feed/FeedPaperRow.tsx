import { memo } from 'react';
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { FeedbackButtons } from '@/components/shared/FeedbackButtons';
import { CitationMenu } from '@/components/citation/CitationMenu';
import { formatAuthors, formatDate } from '@/lib/utils';
import { type FeedPaper, type SurfaceView } from '@/types';
import { priorityLevel } from '@/lib/priority';

// Map lifecycle state to badge colour classes (B.2)
const STATE_BADGE_CLASSES: Record<string, string> = {
  inbox: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200',
  to_read: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200',
  reading: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200',
  done: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  trash: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
};

const STATE_TOOLTIP: Record<string, string> = {
  inbox: 'State: Inbox — New, unread paper. Save to add to your Reading List, or Skip to archive.',
  to_read:
    'State: Reading List — Saved for later. Mark Reading when you start, or Mark Done when finished. Use Pause reading to push back to Reading List.',
  reading:
    'State: Reading — Currently reading. Mark Done when finished, or Pause reading to push back to Reading List.',
  done: 'State: Done — Finished reading. Resume reading to move back to Reading.',
  trash: 'State: Trash — Archived paper. Restore to return it, or permanently delete.',
};

export interface FeedPaperRowProps {
  paper: FeedPaper;
  surface?: SurfaceView;
  // Bulk selection
  isSelected?: boolean;
  onToggleSelect?: (paperId: number) => void;
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

function FeedPaperRowInner({
  paper,
  surface: _surface,
  isSelected,
  onToggleSelect,
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

  const effectiveSelected = isSelected ?? false;
  const effectiveToggle = onToggleSelect;

  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-col gap-3 sm:flex-row">
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
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-semibold leading-tight">{paper.title}</h3>
            {state === 'inbox' && (
              <Badge variant="default" className="text-xs">
                NEW
              </Badge>
            )}
            {isStarred && (
              <span className="text-amber-400" title="Starred">
                ★
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">{formatAuthors(paper.authors)}</p>
          {paper.tldr && <p className="mt-2 text-sm italic">{paper.tldr}</p>}
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

        <div className="flex flex-row flex-wrap items-center gap-2 sm:shrink-0 sm:flex-col sm:items-end sm:gap-1">
          <Badge variant="outline">{paper.source_type.toUpperCase()}</Badge>
          {/* Pulse-origin papers also appear in Inbox; this badge makes the
              overlap legible without separating the data models. */}
          {(paper.discovery_origin === 'pulse' || paper.discovery_origin === 'recommender') && (
            <Badge
              variant="secondary"
              className="bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200"
              title={
                paper.discovery_origin === 'pulse'
                  ? "Also in today's Pulse Deck"
                  : 'Suggested by AI based on your interests'
              }
            >
              ✦ {paper.discovery_origin === 'pulse' ? 'Pulse' : 'Recommended'}
            </Badge>
          )}
          {/* B.2 — Colored state badge with Radix Tooltip explaining transitions */}
          <TooltipProvider delayDuration={150}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="outline" className={STATE_BADGE_CLASSES[state] ?? ''}>
                  {state.toUpperCase()}
                </Badge>
              </TooltipTrigger>
              <TooltipContent side="left" className="max-w-xs text-xs">
                {STATE_TOOLTIP[state] ?? `State: ${state}`}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
          {paper.confidence && (
            <Badge variant={paper.confidence === 'HIGH' ? 'default' : 'secondary'}>
              {paper.confidence}
            </Badge>
          )}
          <PriorityBadge level={priorityLevel(paper.priority_score ?? null)} />
          <div className="flex gap-1">
            {/* FeedPaper has no pdf_downloaded/has_chunks/has_summary at top-level — they come from LifecyclePaperResponse extensions */}
            {paper.has_chunks && (
              <TooltipProvider delayDuration={150}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge variant="outline" className="px-1.5 py-0 text-xs">
                      Processed
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="text-xs">
                    PDF text prepared for AI search
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
            {paper.has_summary && (
              <Badge variant="outline" className="px-1.5 py-0 text-xs">
                Summary
              </Badge>
            )}
          </div>
          {paper.recommendation_score != null && paper.recommendation_reason && (
            <Badge variant="outline" className="border-blue-300 bg-blue-50 text-xs text-blue-600">
              ★ {paper.recommendation_reason}
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">
            {formatDate(paper.published_date ?? paper.created_at)}
            {paper.published_date && (
              <span
                className="ml-1 text-xs text-muted-foreground/60"
                title={`Added: ${formatDate(paper.created_at)}`}
              >
                (added {formatDate(paper.created_at)})
              </span>
            )}
          </span>

          {/* State-switch action button bar — icon-only buttons use Radix Tooltip (B.3) */}
          <TooltipProvider delayDuration={150}>
            <div className="mt-auto flex flex-wrap gap-1">
              {state === 'inbox' && (
                <>
                  {onSave && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onSave(paper.id)}
                          aria-label={`Save ${paper.title}`}
                        >
                          <Save className="mr-1 h-3 w-3" />
                          Save
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Save to Reading List
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {onSkip && (
                    /* B.5 — Skip tooltip clarifying destination state */
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onSkip(paper.id)}
                          aria-label={`Skip ${paper.title}`}
                        >
                          <SkipForward className="mr-1 h-3 w-3" />
                          Skip
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="max-w-xs text-xs">
                        Skip — moves paper to Done without saving to your Reading List. Use Trash if
                        you don&apos;t want this paper at all.
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {onTrash && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="icon"
                          onClick={() => onTrash(paper.id)}
                          aria-label={`Trash ${paper.title}`}
                          className="h-9 w-9"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Move to Trash
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {isStarred
                    ? onUnstar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onUnstar(paper.id)}
                              aria-label={`Unstar ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <StarOff className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Unstar
                          </TooltipContent>
                        </Tooltip>
                      )
                    : onStar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onStar(paper.id)}
                              aria-label={`Star ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <Star className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Star
                          </TooltipContent>
                        </Tooltip>
                      )}
                </>
              )}

              {state === 'to_read' && (
                <>
                  {onMarkReading && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onMarkReading(paper.id)}
                          aria-label={`Mark ${paper.title} as reading`}
                        >
                          <BookOpen className="mr-1 h-3 w-3" />
                          Mark Reading
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Move to Reading — actively reading
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {onMarkDone && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onMarkDone(paper.id)}
                          aria-label={`Mark ${paper.title} as done`}
                        >
                          <CheckCircle className="mr-1 h-3 w-3" />
                          Mark Done
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Mark as finished — moves to Done
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {onTrash && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="icon"
                          onClick={() => onTrash(paper.id)}
                          aria-label={`Trash ${paper.title}`}
                          className="h-9 w-9"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Move to Trash
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {isStarred
                    ? onUnstar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onUnstar(paper.id)}
                              aria-label={`Unstar ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <StarOff className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Unstar
                          </TooltipContent>
                        </Tooltip>
                      )
                    : onStar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onStar(paper.id)}
                              aria-label={`Star ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <Star className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Star
                          </TooltipContent>
                        </Tooltip>
                      )}
                </>
              )}

              {state === 'reading' && (
                <>
                  {onSetAside && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onSetAside(paper.id)}
                          aria-label={`Pause reading ${paper.title}`}
                        >
                          <Library className="mr-1 h-3 w-3" />
                          Pause reading
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Push back to Reading List (Reading → To Read)
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {onMarkDone && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onMarkDone(paper.id)}
                          aria-label={`Mark ${paper.title} as done`}
                        >
                          <CheckCircle className="mr-1 h-3 w-3" />
                          Mark Done
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Mark as finished — moves to Done
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {onTrash && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="icon"
                          onClick={() => onTrash(paper.id)}
                          aria-label={`Trash ${paper.title}`}
                          className="h-9 w-9"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Move to Trash
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {isStarred
                    ? onUnstar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onUnstar(paper.id)}
                              aria-label={`Unstar ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <StarOff className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Unstar
                          </TooltipContent>
                        </Tooltip>
                      )
                    : onStar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onStar(paper.id)}
                              aria-label={`Star ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <Star className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Star
                          </TooltipContent>
                        </Tooltip>
                      )}
                </>
              )}

              {state === 'done' && (
                <>
                  {onReopen && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onReopen(paper.id)}
                          aria-label={`Resume reading ${paper.title}`}
                        >
                          <RotateCcw className="mr-1 h-3 w-3" />
                          Resume reading
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Resume reading — moves Done → Reading
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {onTrash && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="icon"
                          onClick={() => onTrash(paper.id)}
                          aria-label={`Trash ${paper.title}`}
                          className="h-9 w-9"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Move to Trash
                      </TooltipContent>
                    </Tooltip>
                  )}
                  {isStarred
                    ? onUnstar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onUnstar(paper.id)}
                              aria-label={`Unstar ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <StarOff className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Unstar
                          </TooltipContent>
                        </Tooltip>
                      )
                    : onStar && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() => onStar(paper.id)}
                              aria-label={`Star ${paper.title}`}
                              className="h-9 w-9"
                            >
                              <Star className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Star
                          </TooltipContent>
                        </Tooltip>
                      )}
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
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => onHardDelete(paper.id)}
                          aria-label={`Permanently delete ${paper.title}`}
                        >
                          <X className="mr-1 h-3 w-3" />
                          Permanently delete
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Permanently delete this paper (cannot be undone)
                      </TooltipContent>
                    </Tooltip>
                  )}
                </>
              )}

              <CitationMenu paperIds={[paper.id]} />

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
          </TooltipProvider>

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

/**
 * Memoized to prevent re-renders of unrelated rows when sibling
 * row state changes (e.g. one row's mutation fires → only that row re-renders).
 */
export const FeedPaperRow = memo(FeedPaperRowInner);
FeedPaperRow.displayName = 'FeedPaperRow';

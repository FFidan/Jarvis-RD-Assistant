import { Archive, CheckCircle, Eye, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { formatAuthors, formatDate } from '@/lib/utils';
import { type FeedPaper, priorityLevel } from '@/types';

interface FeedPaperRowProps {
  paper: FeedPaper;
  seedChecked?: boolean;
  onSeedChange?: (paperId: number) => void;
  onMarkRead?: (paperId: number) => void;
  markReadPending?: boolean;
  onArchive?: (paperId: number) => void;
  archivePending?: boolean;
  onView?: (paperId: number) => void;
  viewLabel?: string;
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
}: FeedPaperRowProps) {
  const statusLabel = paper.user_status || 'new';

  return (
    <div className="rounded-lg border p-4">
      <div className="flex gap-3">
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
          <h3 className="text-lg font-semibold leading-tight">{paper.title}</h3>
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
          <Badge variant="secondary">{statusLabel.toUpperCase()}</Badge>
          {paper.confidence && (
            <Badge variant={paper.confidence === 'HIGH' ? 'default' : 'secondary'}>
              {paper.confidence}
            </Badge>
          )}
          <PriorityBadge level={priorityLevel(paper.priority_score)} />
          <div className="flex gap-1">
            {paper.pdf_downloaded && <Badge variant="outline" className="text-xs px-1.5 py-0">PDF</Badge>}
            {paper.has_chunks && <Badge variant="outline" className="text-xs px-1.5 py-0">Chunked</Badge>}
            {paper.has_summary && <Badge variant="outline" className="text-xs px-1.5 py-0">Summary</Badge>}
          </div>
          {paper.recommendation_score != null && paper.recommendation_reason && (
            <Badge variant="outline" className="text-xs text-blue-600 border-blue-300 bg-blue-50">
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
          <div className="mt-auto flex gap-2">
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

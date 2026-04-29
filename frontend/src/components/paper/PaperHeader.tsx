import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { type Paper, type UserState, priorityLevel } from '@/types';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { HardDeleteModal } from '@/components/feed/HardDeleteModal';
import { formatDate, formatAuthors } from '@/lib/utils';
import { ExternalLink, Star, Bookmark, BookmarkCheck, BookOpen, Archive, Trash2, Trash } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { bookmarkPaper, savePaper, unsavePaper, markPaperRead, archivePaper, dismissPaper } from '@/lib/api';
import { toast } from 'sonner';

interface PaperHeaderProps {
  paper: Paper;
  /** Whether this paper is currently starred/bookmarked */
  isStarred?: boolean;
  /** Current user_state for the paper — drives Save/Archive/Dismiss/Read button states */
  userState?: UserState | null;
}

export function PaperHeader({ paper, isStarred = false, userState }: PaperHeaderProps) {
  const isValidUrl =
    paper.url && (paper.url.startsWith('http://') || paper.url.startsWith('https://'));

  const [searchParams] = useSearchParams();
  const isTrashSurface = searchParams.get('surface') === 'trash';

  const [hardDeleteOpen, setHardDeleteOpen] = useState(false);

  const queryClient = useQueryClient();

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['paper-detail', paper.id] });
    queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
  };

  const bookmarkMut = useMutation({
    mutationFn: () => bookmarkPaper(paper.id),
    onSuccess: invalidate,
    onError: () => toast.error('Failed to bookmark paper'),
  });

  const isSaved = userState?.saved === true;
  const saveMut = useMutation({
    mutationFn: () => (isSaved ? unsavePaper(paper.id) : savePaper(paper.id)),
    onSuccess: () => {
      invalidate();
      toast.success(isSaved ? 'Removed from library' : 'Saved to library');
    },
    onError: () => toast.error('Failed to update saved state'),
  });

  const isRead = userState?.status === 'read';
  const readMut = useMutation({
    mutationFn: () => markPaperRead(paper.id),
    onSuccess: () => {
      invalidate();
      toast.success(isRead ? 'Marked as reading' : 'Marked as read');
    },
    onError: () => toast.error('Failed to mark as read'),
  });

  const isArchived = userState?.archived === true;
  const archiveMut = useMutation({
    mutationFn: () => archivePaper(paper.id),
    onSuccess: () => {
      invalidate();
      toast.success(isArchived ? 'Unarchived' : 'Archived');
    },
    onError: () => toast.error('Failed to archive paper'),
  });

  const isDismissed = userState?.dismissed === true;
  const dismissMut = useMutation({
    mutationFn: () => dismissPaper(paper.id),
    onSuccess: () => {
      invalidate();
      toast.success('Moved to Trash');
    },
    onError: () => toast.error('Failed to dismiss paper'),
  });

  const handleDismiss = () => {
    toast.warning('Move to Trash?', {
      action: {
        label: 'Confirm',
        onClick: () => dismissMut.mutate(),
      },
    });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-bold leading-tight lg:text-3xl">{paper.title}</h1>
        <div className="flex items-center gap-2 shrink-0">
          {/* Star / Bookmark */}
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={() => bookmarkMut.mutate()}
            disabled={bookmarkMut.isPending}
            title={isStarred ? 'Bookmarked (starred)' : 'Bookmark this paper'}
            aria-label={isStarred ? 'Bookmarked' : 'Bookmark paper'}
          >
            <Star
              className={`h-4 w-4 ${isStarred ? 'fill-yellow-400 text-yellow-400' : 'text-muted-foreground'}`}
            />
          </Button>

          {/* Save / Unsave */}
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending}
            title={isSaved ? 'Saved — click to unsave' : 'Save to library'}
            aria-label={isSaved ? 'Saved' : 'Save paper'}
          >
            {isSaved ? (
              <BookmarkCheck className="h-4 w-4 fill-blue-500 text-blue-500" />
            ) : (
              <Bookmark className="h-4 w-4 text-muted-foreground" />
            )}
          </Button>

          {/* Mark Read / Reading */}
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={() => readMut.mutate()}
            disabled={readMut.isPending}
            title={isRead ? 'Read — click to toggle' : 'Mark as read'}
            aria-label={isRead ? 'Read' : 'Mark as read'}
          >
            <BookOpen
              className={`h-4 w-4 ${isRead ? 'fill-green-500 text-green-500' : 'text-muted-foreground'}`}
            />
          </Button>

          {/* Archive / Unarchive — only enabled when saved */}
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={() => archiveMut.mutate()}
            disabled={archiveMut.isPending || !isSaved}
            title={!isSaved ? 'Save the paper first to archive it' : isArchived ? 'Unarchive' : 'Archive'}
            aria-label={isArchived ? 'Unarchive' : 'Archive'}
          >
            <Archive
              className={`h-4 w-4 ${isArchived ? 'fill-orange-400 text-orange-400' : !isSaved ? 'opacity-30 text-muted-foreground' : 'text-muted-foreground'}`}
            />
          </Button>

          {/* Dismiss → Trash */}
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={handleDismiss}
            disabled={dismissMut.isPending}
            title="Move to Trash"
            aria-label="Dismiss paper"
          >
            <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
          </Button>

          {/* Hard Delete — only on ?surface=trash AND when dismissed */}
          {isTrashSurface && isDismissed && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 w-8 p-0 text-destructive hover:text-destructive"
              onClick={() => setHardDeleteOpen(true)}
              title="Delete forever"
              aria-label="Delete paper forever"
            >
              <Trash className="h-4 w-4" />
            </Button>
          )}

          <PriorityBadge level={priorityLevel(paper.priority_score)} />
        </div>
      </div>

      {paper.authors.length > 0 && (
        <p className="text-sm text-muted-foreground">{formatAuthors(paper.authors)}</p>
      )}

      <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
        <Badge variant="outline">{paper.source_type}</Badge>
        <span>Published: {formatDate(paper.published_date ?? paper.created_at)}</span>
        {paper.citation_count > 0 && (
          <Badge variant="secondary">{paper.citation_count} citations</Badge>
        )}
        {isValidUrl && (
          <Button variant="link" size="sm" className="h-auto p-0" asChild>
            <a href={paper.url} target="_blank" rel="noopener noreferrer">
              Open original <ExternalLink className="ml-1 h-3 w-3" />
            </a>
          </Button>
        )}
      </div>

      <HardDeleteModal
        open={hardDeleteOpen}
        paperId={paper.id}
        paperTitle={paper.title}
        onClose={() => setHardDeleteOpen(false)}
      />
    </div>
  );
}

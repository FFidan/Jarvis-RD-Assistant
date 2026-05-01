import { useState } from 'react';
import { type Paper, priorityLevel, type LifecycleState } from '@/types';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { HardDeleteModal } from '@/components/feed/HardDeleteModal';
import { formatDate, formatAuthors } from '@/lib/utils';
import {
  ExternalLink,
  Star,
  StarOff,
  Save,
  SkipForward,
  BookOpen,
  Library,
  CheckCircle,
  RotateCcw,
  ArchiveRestore,
  Trash2,
  Trash,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  savePaper,
  skipPaper,
  markReading,
  markDone,
  trashPaper,
  restorePaper,
  starPaper,
  unstarPaper,
  hardDeletePaper,
} from '@/lib/api';
import { toast } from 'sonner';

/** Minimal user_state shape needed for lifecycle rendering. Accepts both legacy and Phase-A shapes. */
interface UserStateLike {
  state?: LifecycleState;
  starred?: boolean;
}

interface PaperHeaderProps {
  // Note: Paper now carries optional discovery_origin and recent_feedback
  // (added in Wave 7); FeedbackButtons moved to ActionsSidebar so
  // PaperHeader no longer reads either field, but the underlying Paper
  // type still allows them on the parent's payload.
  paper: Paper & {
    user_state?: UserStateLike | null;
  };
  /** @deprecated Pass lifecycle fields on paper.user_state instead. Kept for backward compat during Wave 2.2. */
  isStarred?: boolean;
  /** @deprecated Pass lifecycle fields on paper.user_state instead. Kept for backward compat during Wave 2.2. */
  userState?: UserStateLike | null;
}

export function PaperHeader({ paper, isStarred: isStarredProp = false, userState: userStateProp }: PaperHeaderProps) {
  const isValidUrl =
    paper.url && (paper.url.startsWith('http://') || paper.url.startsWith('https://'));

  const [hardDeleteOpen, setHardDeleteOpen] = useState(false);

  const queryClient = useQueryClient();

  // Lifecycle state: prefer paper.user_state.state, fall back to prop, then inbox
  const resolvedUserState = paper.user_state ?? userStateProp ?? null;
  const state: LifecycleState = resolvedUserState?.state ?? 'inbox';
  const isStarred = resolvedUserState?.starred ?? isStarredProp;

  // NI-3 error helper
  const toastError = (verb: string) => (err: unknown) =>
    toast.error(`Failed to ${verb}`, {
      description: err instanceof Error ? err.message : 'Unknown error',
    });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
    queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
    queryClient.invalidateQueries({ queryKey: ['paper-detail', paper.id] });
  };

  const saveMut = useMutation({
    mutationFn: () => savePaper(paper.id),
    onSuccess: invalidate,
    onError: toastError('save'),
  });

  const skipMut = useMutation({
    mutationFn: () => skipPaper(paper.id),
    onSuccess: invalidate,
    onError: toastError('skip'),
  });

  const markReadingMut = useMutation({
    mutationFn: () => markReading(paper.id),
    onSuccess: invalidate,
    onError: toastError('mark as reading'),
  });

  const markDoneMut = useMutation({
    mutationFn: () => markDone(paper.id),
    onSuccess: invalidate,
    onError: toastError('mark as done'),
  });

  const trashMut = useMutation({
    mutationFn: () => trashPaper(paper.id),
    onSuccess: invalidate,
    onError: toastError('trash'),
  });

  const restoreMut = useMutation({
    mutationFn: () => restorePaper(paper.id),
    onSuccess: invalidate,
    onError: toastError('restore'),
  });

  const starMut = useMutation({
    mutationFn: () => (isStarred ? unstarPaper(paper.id) : starPaper(paper.id)),
    onSuccess: invalidate,
    onError: toastError(isStarred ? 'unstar' : 'star'),
  });

  const hardDeleteMut = useMutation({
    mutationFn: () => hardDeletePaper(paper.id),
    onSuccess: () => {
      invalidate();
    },
    onError: toastError('delete'),
  });

  const handleTrash = () => {
    toast.warning('Move to Trash?', {
      action: {
        label: 'Confirm',
        onClick: () => trashMut.mutate(),
      },
    });
  };

  // State-contextual primary/secondary action buttons
  const renderActionButtons = () => {
    if (state === 'inbox') {
      return (
        <>
          <Button
            variant="default"
            size="sm"
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending}
            title="Save to reading list"
            aria-label="Save paper"
          >
            <Save className="mr-1 h-4 w-4" />
            Save
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => skipMut.mutate()}
            disabled={skipMut.isPending}
            title="Skip this paper"
            aria-label="Skip paper"
          >
            <SkipForward className="h-4 w-4" />
          </Button>
        </>
      );
    }

    if (state === 'to_read') {
      return (
        <Button
          variant="default"
          size="sm"
          onClick={() => markReadingMut.mutate()}
          disabled={markReadingMut.isPending}
          title="Start reading"
          aria-label="Mark as reading"
        >
          <BookOpen className="mr-1 h-4 w-4" />
          Start Reading
        </Button>
      );
    }

    if (state === 'reading') {
      return (
        <>
          <Button
            variant="default"
            size="sm"
            onClick={() => markDoneMut.mutate()}
            disabled={markDoneMut.isPending}
            title="Mark as done"
            aria-label="Mark as done"
          >
            <CheckCircle className="mr-1 h-4 w-4" />
            Mark Done
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending}
            title="Set aside (back to reading list)"
            aria-label="Set aside"
          >
            <Library className="h-4 w-4" />
          </Button>
        </>
      );
    }

    if (state === 'done') {
      return (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => markReadingMut.mutate()}
          disabled={markReadingMut.isPending}
          title="Re-open for reading"
          aria-label="Re-open paper"
        >
          <RotateCcw className="mr-1 h-4 w-4" />
          Re-open
        </Button>
      );
    }

    if (state === 'trash') {
      return (
        <>
          <Button
            variant="default"
            size="sm"
            onClick={() => restoreMut.mutate()}
            disabled={restoreMut.isPending}
            title="Restore from trash"
            aria-label="Restore paper"
          >
            <ArchiveRestore className="mr-1 h-4 w-4" />
            Restore
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            onClick={() => setHardDeleteOpen(true)}
            disabled={hardDeleteMut.isPending}
            title="Delete forever"
            aria-label="Delete paper forever"
          >
            <Trash className="h-4 w-4" />
          </Button>
        </>
      );
    }

    return null;
  };

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-bold leading-tight lg:text-3xl">{paper.title}</h1>
        <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
          {/* State-contextual action buttons */}
          {renderActionButtons()}

          {/* Star toggle — always shown except trash */}
          {state !== 'trash' && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 w-8 p-0"
              onClick={() => starMut.mutate()}
              disabled={starMut.isPending}
              title={isStarred ? 'Starred — click to unstar' : 'Star this paper'}
              aria-label={isStarred ? 'Starred' : 'Star paper'}
            >
              {isStarred ? (
                <StarOff className="h-4 w-4 text-yellow-400" />
              ) : (
                <Star className="h-4 w-4 text-muted-foreground" />
              )}
            </Button>
          )}

          {/* Trash — shown on all non-trash states */}
          {state !== 'trash' && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 w-8 p-0"
              onClick={handleTrash}
              disabled={trashMut.isPending}
              title="Move to Trash"
              aria-label="Trash paper"
            >
              <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
            </Button>
          )}

          {/* Feedback thumbs moved to ActionsSidebar in Wave 7 (B.5) per spec §5.2 line 349. */}
          <PriorityBadge level={priorityLevel(paper.priority_score ?? null)} />
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
        onOpenChange={setHardDeleteOpen}
        paperId={paper.id}
        paperTitle={paper.title}
      />
    </div>
  );
}

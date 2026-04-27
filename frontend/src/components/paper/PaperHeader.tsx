import { type Paper, priorityLevel } from '@/types';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { formatDate, formatAuthors } from '@/lib/utils';
import { ExternalLink, Star } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { bookmarkPaper } from '@/lib/api';
import { toast } from 'sonner';

interface PaperHeaderProps {
  paper: Paper;
  /** Whether this paper is currently starred/bookmarked */
  isStarred?: boolean;
}

export function PaperHeader({ paper, isStarred = false }: PaperHeaderProps) {
  const isValidUrl =
    paper.url && (paper.url.startsWith('http://') || paper.url.startsWith('https://'));

  const queryClient = useQueryClient();
  const bookmarkMut = useMutation({
    mutationFn: () => bookmarkPaper(paper.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['paper-detail', paper.id] });
    },
    onError: () => {
      toast.error('Failed to bookmark paper');
    },
  });

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-bold leading-tight lg:text-3xl">{paper.title}</h1>
        <div className="flex items-center gap-2 shrink-0">
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
    </div>
  );
}

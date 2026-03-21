import { type Paper, priorityLevel } from '@/types';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { formatDate, formatAuthors } from '@/lib/utils';
import { ExternalLink } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface PaperHeaderProps {
  paper: Paper;
}

export function PaperHeader({ paper }: PaperHeaderProps) {
  const isValidUrl =
    paper.url && (paper.url.startsWith('http://') || paper.url.startsWith('https://'));

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-bold leading-tight lg:text-3xl">{paper.title}</h1>
        <PriorityBadge level={priorityLevel(paper.priority_score)} />
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

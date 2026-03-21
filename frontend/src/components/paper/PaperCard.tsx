import { type Paper, type FeedPaper, priorityLevel } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { formatDate, formatAuthors } from '@/lib/utils';

interface PaperCardProps {
  paper: Paper | FeedPaper;
  onClick?: () => void;
}

function isFeedPaper(paper: Paper | FeedPaper): paper is FeedPaper {
  return 'summary_brief' in paper;
}

export function PaperCard({ paper, onClick }: PaperCardProps) {
  return (
    <Card
      className="cursor-pointer transition-shadow hover:shadow-md"
      onClick={onClick}
    >
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="line-clamp-2 text-base">{paper.title}</CardTitle>
          <PriorityBadge level={priorityLevel(paper.priority_score)} />
        </div>
        <p className="text-sm text-muted-foreground">
          {formatAuthors(paper.authors)} &middot; {formatDate(paper.published_date)}
        </p>
      </CardHeader>
      <CardContent>
        {isFeedPaper(paper) && paper.tldr && (
          <p className="mb-2 text-sm">{paper.tldr}</p>
        )}
        {isFeedPaper(paper) && !paper.tldr && paper.summary_brief && (
          <p className="mb-2 line-clamp-3 text-sm">{paper.summary_brief}</p>
        )}
        {!isFeedPaper(paper) && paper.abstract && (
          <p className="mb-2 line-clamp-3 text-sm text-muted-foreground">{paper.abstract}</p>
        )}
        <div className="flex flex-wrap gap-1">
          <Badge variant="outline">{paper.source_type}</Badge>
          {paper.citation_count > 0 && (
            <Badge variant="secondary">{paper.citation_count} citations</Badge>
          )}
          {isFeedPaper(paper) && paper.confidence && (
            <Badge variant={paper.confidence === 'HIGH' ? 'default' : 'secondary'}>
              {paper.confidence}
            </Badge>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

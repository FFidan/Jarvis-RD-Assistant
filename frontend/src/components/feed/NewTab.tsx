import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { fetchFeedPapers, fetchTopics, markPaperRead } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { priorityLevel } from '@/types';
import { PaginationControls } from '@/components/feed/PaginationControls';
import { LibraryFilters } from './LibraryFilters';
import { EmptyState } from '@/components/EmptyState';
import { formatAuthors, formatDate } from '@/lib/utils';
import { CheckCircle, Eye, Inbox } from 'lucide-react';

const PAGE_SIZE = 20;

export function NewTab() {
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState('discovered_at');
  const [filterText, setFilterText] = useState('');
  const [selectedStatuses, setSelectedStatuses] = useState<string[]>([]);
  const [selectedSources, setSelectedSources] = useState<string[]>([]);
  const [selectedTopics, setSelectedTopics] = useState<string[]>([]);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: topics = [] } = useQuery({
    queryKey: ['topics'],
    queryFn: fetchTopics,
  });

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['feed', 'new', sort, page, filterText, selectedStatuses, selectedSources, selectedTopics, dateFrom, dateTo],
    queryFn: () =>
      fetchFeedPapers({
        unread_only: true,
        sort: sort as 'discovered_at' | 'priority' | 'published_date' | 'title' | 'citation_count' | 'recommendation',
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        q: filterText || undefined,
        statuses: selectedStatuses.length ? selectedStatuses.join(',') : undefined,
        source_types: selectedSources.length ? selectedSources.join(',') : undefined,
        topic_names: selectedTopics.length ? selectedTopics.join(',') : undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        recommended: undefined,
      }),
  });

  const papers = data?.papers ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const markReadMutation = useMutation({
    mutationFn: markPaperRead,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['feed'] });
    },
  });

  if (isLoading) {
    return (
      <div className="space-y-4 pt-4">
        {[1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-32 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <div className="py-8 text-center">
        <p className="text-sm text-destructive">
          Failed to load data: {(error as Error).message}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4 pt-4">
      <LibraryFilters
        filterText={filterText}
        onFilterTextChange={(v) => { setFilterText(v); setPage(0); }}
        selectedStatuses={selectedStatuses}
        onStatusChange={(v) => { setSelectedStatuses(v); setPage(0); }}
        selectedSources={selectedSources}
        onSourceChange={(v) => { setSelectedSources(v); setPage(0); }}
        selectedTopics={selectedTopics}
        onTopicChange={(v) => { setSelectedTopics(v); setPage(0); }}
        topicOptions={topics.map((t) => t.name)}
        dateFrom={dateFrom}
        onDateFromChange={(v) => { setDateFrom(v); setPage(0); }}
        dateTo={dateTo}
        onDateToChange={(v) => { setDateTo(v); setPage(0); }}
        sortBy={sort}
        onSortChange={(v) => { setSort(v); setPage(0); }}
      />
      {total > 0 && (
        <p className="text-sm text-muted-foreground">
          Showing {papers.length} of {total} unread papers
        </p>
      )}

      {papers.length === 0 ? (
        <EmptyState
          icon={Inbox}
          title="All caught up!"
          description="No unread papers. Search for new papers or add topics in Settings to discover research automatically."
          actionLabel="Go to Settings"
          actionHref="/settings"
        />
      ) : (
        <>
          {papers.map((paper) => (
            <div key={paper.id} className="rounded-lg border p-4">
              <div className="flex gap-4">
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
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <Badge variant="outline">{paper.source_type.toUpperCase()}</Badge>
                  {paper.user_status && (
                    <Badge variant="secondary">
                      {paper.user_status.toUpperCase()}
                    </Badge>
                  )}
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
                  {paper.discovered_at && (
                    <span className="text-xs text-muted-foreground">
                      {formatDate(paper.discovered_at)}
                    </span>
                  )}
                  <div className="mt-auto flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => markReadMutation.mutate(paper.id)}
                      disabled={markReadMutation.isPending}
                    >
                      <CheckCircle className="mr-1 h-3 w-3" />
                      Mark Read
                    </Button>
                    <Button
                      variant="default"
                      size="sm"
                      onClick={() => navigate(`/paper/${paper.id}`)}
                    >
                      <Eye className="mr-1 h-3 w-3" />
                      View
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          ))}

          <PaginationControls
            page={page}
            totalPages={totalPages}
            onPageChange={setPage}
          />
        </>
      )}
    </div>
  );
}

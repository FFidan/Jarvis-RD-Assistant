import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { archivePaper, fetchFeedPapers, fetchTopics, markPaperRead } from '@/lib/api';
import { Skeleton } from '@/components/ui/skeleton';
import { PaginationControls } from '@/components/feed/PaginationControls';
import { LibraryFilters } from './LibraryFilters';
import { EmptyState } from '@/components/EmptyState';
import { FeedPaperRow } from '@/components/feed/FeedPaperRow';
import { Inbox } from 'lucide-react';

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
  const [pendingMarkReadIds, setPendingMarkReadIds] = useState<Set<number>>(new Set());
  const [pendingArchiveIds, setPendingArchiveIds] = useState<Set<number>>(new Set());
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
        include_zotero_notes: true,
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

  const archiveMutation = useMutation({
    mutationFn: archivePaper,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['feed'] });
    },
  });

  const markRowRead = (paperId: number) => {
    setPendingMarkReadIds((prev) => new Set(prev).add(paperId));
    markReadMutation.mutate(paperId, {
      onSettled: () => {
        setPendingMarkReadIds((prev) => {
          const next = new Set(prev);
          next.delete(paperId);
          return next;
        });
      },
    });
  };

  const archiveRow = (paperId: number) => {
    setPendingArchiveIds((prev) => new Set(prev).add(paperId));
    archiveMutation.mutate(paperId, {
      onSettled: () => {
        setPendingArchiveIds((prev) => {
          const next = new Set(prev);
          next.delete(paperId);
          return next;
        });
      },
    });
  };

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
            <FeedPaperRow
              key={paper.id}
              paper={paper}
              onMarkRead={markRowRead}
              markReadPending={pendingMarkReadIds.has(paper.id)}
              onArchive={archiveRow}
              archivePending={pendingArchiveIds.has(paper.id)}
              onView={(paperId) => navigate(`/paper/${paperId}`)}
            />
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

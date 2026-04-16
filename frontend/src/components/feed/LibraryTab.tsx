import { useState, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import {
  fetchFeedPapers,
  fetchTopics,
  scanLocalPdfs,
  batchProcessPapers,
  discoverPapers,
} from '@/lib/api';
import { type DiscoveryResult, priorityLevel } from '@/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PriorityBadge } from '@/components/paper/PriorityBadge';
import { LibraryFilters } from '@/components/feed/LibraryFilters';
import { PaginationControls } from '@/components/feed/PaginationControls';
import { DiscoveryResults } from '@/components/feed/DiscoveryResults';
import { EmptyState } from '@/components/EmptyState';
import { formatAuthors, formatDate } from '@/lib/utils';
import {
  FolderOpen,
  Cog,
  Eye,
  ChevronDown,
  ChevronUp,
  Sparkles,
  Loader2,
  Library,
} from 'lucide-react';

const PAGE_SIZE = 20;

export function LibraryTab() {
  // Import expander state
  const [importExpanded, setImportExpanded] = useState(false);
  const [batchLimit, setBatchLimit] = useState(10);

  // Filter state
  const [filterText, setFilterText] = useState('');
  const [selectedStatuses, setSelectedStatuses] = useState<string[]>([]);
  const [selectedSources, setSelectedSources] = useState<string[]>([]);
  const [selectedTopics, setSelectedTopics] = useState<string[]>([]);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [sortBy, setSortBy] = useState('discovered_at');

  // Pagination
  const [page, setPage] = useState(0);

  // Seed discovery state
  const [seedIds, setSeedIds] = useState<Set<number>>(new Set());
  const [discoveryResults, setDiscoveryResults] = useState<DiscoveryResult[]>([]);

  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Fetch topics for filter dropdown
  const { data: topics = [] } = useQuery({
    queryKey: ['topics'],
    queryFn: fetchTopics,
  });
  const topicNames = topics.map((t) => t.name);

  // Fetch library papers
  const { data, isLoading, isError, error } = useQuery({
    queryKey: [
      'feed',
      'library',
      filterText,
      selectedStatuses,
      selectedSources,
      selectedTopics,
      dateFrom,
      dateTo,
      sortBy,
      page,
    ],
    queryFn: () =>
      fetchFeedPapers({
        sort: sortBy as 'discovered_at' | 'priority' | 'published_date' | 'title' | 'citation_count' | 'recommendation',
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        q: filterText || undefined,
        statuses: selectedStatuses.length > 0 ? selectedStatuses.join(',') : undefined,
        source_types: selectedSources.length > 0 ? selectedSources.join(',') : undefined,
        topic_names: selectedTopics.length > 0 ? selectedTopics.join(',') : undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        recommended: undefined,
      }),
  });

  const papers = data?.papers ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // Mutations
  const scanMutation = useMutation({
    mutationFn: scanLocalPdfs,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['feed'] });
      setPage(0);
    },
  });

  const batchMutation = useMutation({
    mutationFn: () => batchProcessPapers(batchLimit),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['feed'] });
      setPage(0);
    },
  });

  const discoverMutation = useMutation({
    mutationFn: () => discoverPapers(Array.from(seedIds)),
    onSuccess: (data) => {
      setDiscoveryResults(data);
      queryClient.invalidateQueries({ queryKey: ['feed'] });
      setPage(0);
    },
  });

  const toggleSeed = useCallback((id: number) => {
    setSeedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  return (
    <div className="space-y-4 pt-4">
      <h2 className="text-sm font-medium text-muted-foreground mb-2">Your Library{data?.total != null ? ` · ${data.total} papers` : ''}</h2>
      {/* Import Local PDFs Expander */}
      <Card>
        <Button
          variant="ghost"
          className="flex w-full items-center justify-between px-4 py-3"
          onClick={() => setImportExpanded(!importExpanded)}
        >
          <span className="flex items-center gap-2 text-sm font-medium">
            <FolderOpen className="h-4 w-4" />
            Import local PDFs
          </span>
          {importExpanded ? (
            <ChevronUp className="h-4 w-4" />
          ) : (
            <ChevronDown className="h-4 w-4" />
          )}
        </Button>
        {importExpanded && (
          <CardContent className="space-y-4 border-t pt-4">
            <p className="text-sm text-muted-foreground">
              Place PDF files in /data/local_pdfs, then scan to import.
            </p>
            <Button
              onClick={() => scanMutation.mutate()}
              disabled={scanMutation.isPending}
            >
              {scanMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Scan for new PDFs
            </Button>
            {scanMutation.isSuccess && scanMutation.data && (
              <p className="text-sm text-green-600">
                Scan complete &mdash; {scanMutation.data.imported} imported,{' '}
                {scanMutation.data.skipped} skipped ({scanMutation.data.scanned} total scanned).
              </p>
            )}
            {scanMutation.isError && (
              <p className="text-sm text-destructive">{scanMutation.error?.message || 'Scan failed'}</p>
            )}

            <Separator />

            <h4 className="font-medium">Batch Processing</h4>
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2">
                <label htmlFor="batch-limit" className="text-sm">Papers per batch:</label>
                <Input
                  id="batch-limit"
                  type="number"
                  min={1}
                  max={50}
                  value={batchLimit}
                  onChange={(e) => setBatchLimit(Number(e.target.value))}
                  className="w-[80px]"
                />
              </div>
              <Button
                onClick={() => batchMutation.mutate()}
                disabled={batchMutation.isPending}
              >
                {batchMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                <Cog className="mr-2 h-4 w-4" />
                Process Unembedded
              </Button>
            </div>
            {batchMutation.isSuccess && batchMutation.data && (
              <p className="text-sm text-green-600">
                Queued {batchMutation.data.queued} papers for embedding.
                ({batchMutation.data.total_unprocessed} unprocessed found,{' '}
                {batchMutation.data.skipped_missing_pdf} skipped &mdash; missing PDF)
              </p>
            )}
            {batchMutation.isError && (
              <p className="text-sm text-destructive">
                {batchMutation.error instanceof Error ? batchMutation.error.message : 'Batch process failed'}
              </p>
            )}
          </CardContent>
        )}
      </Card>

      {/* Filters */}
      <LibraryFilters
        filterText={filterText}
        onFilterTextChange={(v) => { setFilterText(v); setPage(0); }}
        selectedStatuses={selectedStatuses}
        onStatusChange={(v) => { setSelectedStatuses(v); setPage(0); }}
        selectedSources={selectedSources}
        onSourceChange={(v) => { setSelectedSources(v); setPage(0); }}
        selectedTopics={selectedTopics}
        onTopicChange={(v) => { setSelectedTopics(v); setPage(0); }}
        topicOptions={topicNames}
        dateFrom={dateFrom}
        onDateFromChange={(v) => { setDateFrom(v); setPage(0); }}
        dateTo={dateTo}
        onDateToChange={(v) => { setDateTo(v); setPage(0); }}
        sortBy={sortBy}
        onSortChange={(v) => { setSortBy(v); setPage(0); }}
      />

      {/* Loading */}
      {isLoading && (
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-32 w-full" />
          ))}
        </div>
      )}

      {/* Error State */}
      {isError && (
        <div className="py-8 text-center">
          <p className="text-sm text-destructive">
            Failed to load data: {(error as Error).message}
          </p>
        </div>
      )}

      {/* Papers List */}
      {!isLoading && !isError && papers.length === 0 && (
        <EmptyState
          icon={Library}
          title="No papers in your library"
          description="Add a topic in Settings, then use the search bar above or click 'Fetch Papers' to discover research."
          actionLabel="Go to Settings"
          actionHref="/settings"
        />
      )}

      {!isLoading && !isError && papers.length > 0 && (
        <>
          <p className="text-sm text-muted-foreground">
            Showing {papers.length} of {total} papers
          </p>

          {papers.map((paper) => (
            <div key={paper.id} className="rounded-lg border p-4">
              <div className="flex gap-3">
                {/* Seed checkbox */}
                <div className="flex items-start pt-1">
                  <input
                    type="checkbox"
                    checked={seedIds.has(paper.id)}
                    onChange={() => toggleSeed(paper.id)}
                    className="h-4 w-4 rounded border-gray-300"
                    aria-label={`Select ${paper.title} as seed`}
                    title="Select as seed for discovery"
                  />
                </div>

                {/* Paper info */}
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

                {/* Metadata + actions */}
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <Badge variant="outline">{paper.source_type.toUpperCase()}</Badge>
                  <Badge variant="secondary">
                    {(paper.user_status || 'new').toUpperCase()}
                  </Badge>
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
                  <span className="text-xs text-muted-foreground">
                    {formatDate(paper.published_date || paper.created_at)}
                  </span>
                  <Button
                    variant="default"
                    size="sm"
                    onClick={() => navigate(`/paper/${paper.id}`)}
                    className="mt-auto"
                  >
                    <Eye className="mr-1 h-3 w-3" />
                    View Details
                  </Button>
                </div>
              </div>
            </div>
          ))}

          {/* Seed-based discovery */}
          <div className="flex items-center gap-4">
            <Button
              onClick={() => discoverMutation.mutate()}
              disabled={seedIds.size === 0 || discoverMutation.isPending}
            >
              {discoverMutation.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Sparkles className="mr-2 h-4 w-4" />
              )}
              Find Similar Papers ({seedIds.size} selected)
            </Button>
          </div>

          {/* Discovery results */}
          {discoveryResults.length > 0 && (
            <DiscoveryResults
              results={discoveryResults}
              onClear={() => setDiscoveryResults([])}
            />
          )}

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

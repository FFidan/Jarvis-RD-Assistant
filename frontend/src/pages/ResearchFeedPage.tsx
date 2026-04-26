import { useState, useCallback, useEffect, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  searchPreview,
  batchSavePapers,
  fetchPulseHistory,
  fetchSources,
} from '@/lib/api';
import type { SearchFilters } from '@/lib/api';
import type {
  PulseDeck as PulseDeckType,
  SearchPreviewResult,
  SearchPreviewSourceError,
  SourceConfig,
} from '@/types';
import { toast } from 'sonner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Skeleton } from '@/components/ui/skeleton';
import { StreamingChat } from '@/components/chat/StreamingChat';
import { SearchBar } from '@/components/feed/SearchBar';
import { PreviewResults } from '@/components/feed/PreviewResults';
import { SearchSourceErrors } from '@/components/feed/SearchSourceErrors';
import { SOURCE_LABELS } from '@/components/feed/source-labels';
import { NewTab } from '@/components/feed/NewTab';
import { LibraryTab } from '@/components/feed/LibraryTab';
import { PulseDeck } from '@/components/my-day/PulseDeck';
import { BookOpen } from 'lucide-react';

const VALID_TABS = new Set(['library', 'inbox', 'search', 'ask', 'pulse']);

function TabInfo({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-muted-foreground mb-4">{children}</p>;
}

export function ResearchFeedPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get('tab');
  const [previewResults, setPreviewResults] = useState<SearchPreviewResult[]>([]);
  const [sourceErrors, setSourceErrors] = useState<Record<string, SearchPreviewSourceError>>({});
  const [activeTab, setActiveTab] = useState(
    tabParam && VALID_TABS.has(tabParam) ? tabParam : 'library',
  );
  const [selectedSourceTypes, setSelectedSourceTypes] = useState<string[]>([]);
  const queryClient = useQueryClient();

  // Load enabled external sources to drive the checkbox group
  const { data: allSources } = useQuery<SourceConfig[]>({
    queryKey: ['sources'],
    queryFn: fetchSources,
  });

  // Derive the list of searchable (non-local) enabled sources
  const externalSources = useMemo(
    () => (allSources ?? []).filter((s) => s.source_type !== 'local' && s.enabled),
    [allSources],
  );

  // Initialise selectedSourceTypes once sources load (all checked by default)
  useEffect(() => {
    if (externalSources.length > 0 && selectedSourceTypes.length === 0) {
      setSelectedSourceTypes(externalSources.map((s) => s.source_type));
    }
  }, [externalSources]);

  // Sync tab → URL when user clicks a tab
  const handleTabChange = useCallback(
    (value: string) => {
      setActiveTab(value);
      if (value === 'library') {
        // Don't pollute URL with default tab
        setSearchParams((prev) => {
          prev.delete('tab');
          return prev;
        });
      } else {
        setSearchParams((prev) => {
          prev.set('tab', value);
          return prev;
        });
      }
    },
    [setSearchParams],
  );

  // When the URL ?tab param changes externally (e.g. deep-link navigation),
  // update active tab
  useEffect(() => {
    const t = searchParams.get('tab');
    if (t && VALID_TABS.has(t) && t !== activeTab) {
      setActiveTab(t);
    }
  }, [searchParams]); // intentionally omit activeTab to avoid loop

  const searchMutation = useMutation({
    mutationFn: ({
      query,
      sourceTypes,
      maxResults,
      filters,
    }: {
      query: string;
      sourceTypes: string[];
      maxResults: number;
      filters: SearchFilters;
    }) => searchPreview(query, sourceTypes, maxResults, filters),
    onSuccess: (data) => {
      setPreviewResults(data.results);
      setSourceErrors(data.source_errors ?? {});
    },
    onError: () => {
      setPreviewResults([]);
      setSourceErrors({});
    },
  });

  const saveMutation = useMutation({
    mutationFn: batchSavePapers,
    onSuccess: (data) => {
      const savedByExternalId = new Map(
        data.map((paper) => [paper.external_id, paper.id] as const),
      );

      setPreviewResults((current) =>
        current.map((paper) => {
          const paperId = savedByExternalId.get(paper.external_id);
          if (!paperId) {
            return paper;
          }

          return {
            ...paper,
            library_match: paper.library_match
              ? { ...paper.library_match, paper_id: paperId }
              : {
                  paper_id: paperId,
                  has_project_links: false,
                  zotero_item_key: null,
                },
          };
        }),
      );
      void queryClient.invalidateQueries({ queryKey: ['feed', 'library'] });
      toast.success(`Saved ${data.length} paper(s) to your library.`);
    },
    onError: (error) => {
      const message =
        error instanceof ApiError
          ? error.detail
          : error instanceof Error
            ? error.message
            : 'Save failed. Check service logs.';
      toast.error(message);
    },
  });

  const handleSearch = useCallback((
    query: string,
    sourceTypes: string[],
    maxResults: number,
    filters: SearchFilters,
  ) => {
    searchMutation.mutate({ query, sourceTypes, maxResults, filters });
  }, [searchMutation]);

  function handleSave(papers: SearchPreviewResult[]) {
    saveMutation.mutate(papers);
  }

  function handleClearPreview() {
    setPreviewResults([]);
    setSourceErrors({});
  }

  const searchErrorMessage =
    searchMutation.isError && searchMutation.error
      ? searchMutation.error instanceof ApiError
        ? searchMutation.error.detail
        : searchMutation.error instanceof Error
          ? searchMutation.error.message
          : 'Search failed. Please try again.'
      : null;

  return (
    <div className="space-y-6">
      <h1 className="flex items-center gap-2 text-3xl font-bold">
        <BookOpen className="h-8 w-8" />
        Research Feed
      </h1>
      <p className="text-muted-foreground text-sm">Discover and manage research papers from your configured sources</p>

      <Tabs value={activeTab} onValueChange={handleTabChange}>
        <TabsList>
          <TabsTrigger value="library">Library</TabsTrigger>
          <TabsTrigger value="inbox">Inbox</TabsTrigger>
          <TabsTrigger value="search">Search</TabsTrigger>
          <TabsTrigger value="ask">Ask</TabsTrigger>
          <TabsTrigger value="pulse">Pulse</TabsTrigger>
        </TabsList>

        <TabsContent value="library">
          <TabInfo>Browse, search, and filter all papers in your library.</TabInfo>
          <LibraryTab />
        </TabsContent>

        <TabsContent value="inbox">
          <TabInfo>Unread papers from your configured sources — mark as read, view, or filter.</TabInfo>
          <NewTab />
        </TabsContent>

        <TabsContent value="search">
          <div className="space-y-4">
            <TabInfo>Search external databases live and save new papers to your library.</TabInfo>
            <div>
              <h2 className="text-sm font-medium">Discover New Papers</h2>
              <p className="text-xs text-muted-foreground mb-2">Search across your enabled sources — results can be added to your library.</p>
            </div>

            {/* Multi-source checkbox group */}
            {externalSources.length > 0 && (
              <div className="space-y-1">
                <div className="flex flex-wrap gap-x-4 gap-y-2 items-center">
                  <span className="text-xs font-medium text-muted-foreground">Sources:</span>
                  {externalSources.map((source) => (
                    <label key={source.source_type} className="flex items-center gap-1.5 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 rounded border-gray-300 accent-primary"
                        checked={selectedSourceTypes.includes(source.source_type)}
                        onChange={(e) => {
                          setSelectedSourceTypes((prev) =>
                            e.target.checked
                              ? [...prev, source.source_type]
                              : prev.filter((t) => t !== source.source_type),
                          );
                        }}
                      />
                      <span className="text-sm">{SOURCE_LABELS[source.source_type] ?? source.source_type}</span>
                    </label>
                  ))}
                </div>
                {selectedSourceTypes.length === 0 && (
                  <p className="text-xs text-destructive">Select at least one source</p>
                )}
              </div>
            )}

            <SearchBar
              onSearch={handleSearch}
              isLoading={searchMutation.isPending}
              sourceTypes={selectedSourceTypes}
            />
            {searchErrorMessage && <p className="text-sm text-destructive">{searchErrorMessage}</p>}
            <SearchSourceErrors sourceErrors={sourceErrors} />
            {previewResults.length > 0 && (
              <PreviewResults
                papers={previewResults}
                onSave={handleSave}
                onClear={handleClearPreview}
                isSaving={saveMutation.isPending}
              />
            )}
          </div>
        </TabsContent>

        <TabsContent value="ask" className="flex-1 flex flex-col mt-0">
          <TabInfo>Ask AI questions answered from your indexed paper library.</TabInfo>
          <div className="mb-3">
            <h2 className="text-sm font-medium">Ask Questions</h2>
            <p className="text-xs text-muted-foreground">Get answers synthesised from your entire library.</p>
          </div>
          <div className="flex-1 min-h-[400px]">
            <StreamingChat chatId="cross-paper-rag" scope="cross-paper" />
          </div>
        </TabsContent>

        <TabsContent value="pulse">
          <TabInfo>Your daily AI-curated research briefing and deck history.</TabInfo>
          <div className="space-y-6">
            <PulseDeck />
            <div className="border-t pt-4">
              <h3 className="text-sm font-medium mb-3">Pulse History</h3>
              <PulseHistoryTab />
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function PulseHistoryTab() {
  const { data, isLoading, isError, error } = useQuery<PulseDeckType[]>({
    queryKey: ['pulse-history', 30],
    queryFn: () => fetchPulseHistory(30),
  });

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <p className="text-destructive text-sm">
        Failed to load Pulse history:{' '}
        {error instanceof Error ? error.message : 'unknown error'}
      </p>
    );
  }

  if (!data || data.length === 0) {
    return (
      <p className="text-muted-foreground py-8 text-center text-sm">
        No past Pulse decks yet. Come back after your morning deck has been
        generated.
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {data.map((deck) => (
        <li
          key={deck.deck_id}
          className="hover:bg-muted/30 flex items-center justify-between rounded-lg border p-3 transition-colors"
        >
          <div>
            <p className="font-medium">{deck.deck_date}</p>
            <p className="text-muted-foreground text-xs">
              {deck.card_count} papers · generated{' '}
              {new Date(deck.generated_at).toLocaleString()}
            </p>
          </div>
          <span className="text-muted-foreground text-xs italic">
            read-only
          </span>
        </li>
      ))}
    </ul>
  );
}

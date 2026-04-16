import { useState, useCallback } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  ApiError,
  searchPreview,
  batchSavePapers,
  fetchPulseHistory,
} from '@/lib/api';
import type { PulseDeck as PulseDeckType, SearchPreviewResult } from '@/types';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Skeleton } from '@/components/ui/skeleton';
import { StreamingChat } from '@/components/chat/StreamingChat';
import { SearchBar } from '@/components/feed/SearchBar';
import { PreviewResults } from '@/components/feed/PreviewResults';
import { NewTab } from '@/components/feed/NewTab';
import { LibraryTab } from '@/components/feed/LibraryTab';
import { PulseDeck } from '@/components/my-day/PulseDeck';
import { BookOpen } from 'lucide-react';

export function ResearchFeedPage() {
  const [previewResults, setPreviewResults] = useState<SearchPreviewResult[]>([]);
  const [saveMessage, setSaveMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [activeTab, setActiveTab] = useState('library');
  const [bothSearching, setBothSearching] = useState(false);

  const searchMutation = useMutation({
    mutationFn: ({
      query,
      source,
      maxResults,
    }: {
      query: string;
      source: string;
      maxResults: number;
    }) => searchPreview(query, source, maxResults),
    onSuccess: (data) => {
      setPreviewResults(data);
      setSaveMessage(null);
    },
    onError: () => {
      setPreviewResults([]);
      setSaveMessage(null);
    },
  });

  const saveMutation = useMutation({
    mutationFn: batchSavePapers,
    onSuccess: (data) => {
      setSaveMessage({ type: 'success', text: `Saved ${data.length} paper(s) to your library.` });
      setPreviewResults([]);
      setActiveTab('library');
    },
    onError: () => {
      setSaveMessage({ type: 'error', text: 'Save failed. Check service logs.' });
    },
  });

  const handleSearch = useCallback(async (query: string, source: string, maxResults: number) => {
    if (source === 'both') {
      setBothSearching(true);
      setSaveMessage(null);
      try {
        const [arxivResult, s2Result] = await Promise.allSettled([
          searchPreview(query, 'arxiv', maxResults),
          searchPreview(query, 'semantic_scholar', maxResults),
        ]);
        const arxivPapers = arxivResult.status === 'fulfilled' ? arxivResult.value : [];
        const s2Papers = s2Result.status === 'fulfilled' ? s2Result.value : [];
        const seen = new Set<string>();
        const merged: SearchPreviewResult[] = [];
        for (const paper of [...arxivPapers, ...s2Papers]) {
          if (!seen.has(paper.external_id)) {
            seen.add(paper.external_id);
            merged.push(paper);
          }
        }
        setPreviewResults(merged);
      } catch {
        setPreviewResults([]);
      } finally {
        setBothSearching(false);
      }
    } else {
      searchMutation.mutate({ query, source, maxResults });
    }
  }, [searchMutation]);

  function handleSave(papers: SearchPreviewResult[]) {
    saveMutation.mutate(papers);
  }

  function handleClearPreview() {
    setPreviewResults([]);
    setSaveMessage(null);
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

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="library">Library</TabsTrigger>
          <TabsTrigger value="new">New</TabsTrigger>
          <TabsTrigger value="discover">Discover</TabsTrigger>
          <TabsTrigger value="ask">Ask</TabsTrigger>
          <TabsTrigger value="pulse">Pulse</TabsTrigger>
        </TabsList>

        <TabsContent value="library">
          <LibraryTab />
        </TabsContent>

        <TabsContent value="new">
          <NewTab />
        </TabsContent>

        <TabsContent value="discover">
          <div className="space-y-4">
            <div>
              <h2 className="text-sm font-medium">Discover New Papers</h2>
              <p className="text-xs text-muted-foreground mb-2">Search ArXiv, Semantic Scholar and other sources — results can be added to your library.</p>
            </div>
            <SearchBar onSearch={handleSearch} isLoading={bothSearching || searchMutation.isPending} />
            {searchErrorMessage && <p className="text-sm text-destructive">{searchErrorMessage}</p>}
            {saveMessage && (
              <p className={`text-sm ${saveMessage.type === 'success' ? 'text-green-600' : 'text-destructive'}`}>
                {saveMessage.text}
              </p>
            )}
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
          <div className="mb-3">
            <h2 className="text-sm font-medium">Ask Questions</h2>
            <p className="text-xs text-muted-foreground">Get answers synthesised from your entire library.</p>
          </div>
          <div className="flex-1 min-h-[400px]">
            <StreamingChat chatId="cross-paper-rag" scope="cross-paper" />
          </div>
        </TabsContent>

        <TabsContent value="pulse">
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

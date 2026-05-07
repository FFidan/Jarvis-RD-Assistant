import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  searchPreview,
  batchSavePapers,
  fetchSources,
  useFeedCounts,
} from '@/lib/api';
import type { SearchFilters } from '@/lib/api';
import type {
  SearchPreviewResult,
  SearchPreviewSourceError,
  SourceConfig,
} from '@/types';
import type { SurfaceView, LibraryFilter, FeedCountsResponse, InboxSourceFilter } from '@/types';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { StreamingChat } from '@/components/chat/StreamingChat';
import { SearchBar } from '@/components/feed/SearchBar';
import { PreviewResults } from '@/components/feed/PreviewResults';
import { SearchSourceErrors } from '@/components/feed/SearchSourceErrors';
import { SOURCE_LABELS } from '@/components/feed/source-labels';
import { FeedView } from '@/components/feed/FeedView';
import { CountsBadge } from '@/components/feed/CountsBadge';
import { useBulkSelection } from '@/stores/bulk-selection-store';
import { BookOpen as BookOpenIcon, Star, Library as LibraryIcon, CheckCircle } from 'lucide-react';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';

// ─── surface definitions ────────────────────────────────────────────────────

const SURFACES: { value: SurfaceView; label: string; countsKey?: keyof FeedCountsResponse }[] = [
  { value: 'inbox', label: 'Inbox', countsKey: 'inbox' },
  { value: 'library', label: 'Library', countsKey: 'library' },
  { value: 'search', label: 'Search' },
  { value: 'ask', label: 'Ask' },
  { value: 'trash', label: 'Trash', countsKey: 'trash' },
];

// URL-param guards (M16) — reject unexpected ?surface= / ?filter= values
// Tightened to 5 top-level surfaces; starred/archived/reading are sub-filters only.
const VALID_SURFACES: ReadonlySet<SurfaceView> = new Set<SurfaceView>([
  'inbox',
  'library',
  'search',
  'ask',
  'trash',
]);

const VALID_FILTERS: ReadonlySet<LibraryFilter> = new Set<LibraryFilter>([
  'starred',
  'reading',
  'to_read',
  'done',
]);

// Library sub-chip definitions (spec §5.4 — 5 items: All + 4 filters)
// B.4: tooltip explains lifecycle context for Reading List / Reading chips.
const LIBRARY_SUB_CHIPS: Array<{
  value: LibraryFilter | undefined;
  label: string;
  icon: React.ReactNode;
  tooltip?: string;
}> = [
  {
    value: undefined,
    label: 'All',
    icon: null,
    tooltip: 'Every paper across all states (Inbox, Reading List, Reading, Done — excludes Trash).',
  },
  {
    value: 'starred',
    label: 'Starred',
    icon: <Star size={14} />,
    tooltip: "Papers you've marked as important. Star is orthogonal to lifecycle state.",
  },
  {
    value: 'reading',
    label: 'Reading',
    icon: <BookOpenIcon size={14} />,
    tooltip:
      "Papers you're actively reading. Mark Done when finished, or Set Aside to push back to Reading List.",
  },
  {
    value: 'to_read',
    label: 'Reading List',
    icon: <LibraryIcon size={14} />,
    tooltip:
      "Papers you've saved to read later. Move to Reading when you start, or Done when finished.",
  },
  {
    value: 'done',
    label: 'Done',
    icon: <CheckCircle size={14} />,
    tooltip: "Papers you've finished reading. Re-open to move back to Reading.",
  },
];

const INBOX_SOURCE_CHIPS: Array<{
  value: InboxSourceFilter | undefined;
  label: string;
}> = [
  { value: undefined, label: 'All sources' },
  { value: 'arxiv', label: 'arXiv' },
  { value: 'semantic_scholar', label: 'Semantic Scholar' },
  { value: 'openalex', label: 'OpenAlex' },
  { value: 'pubmed', label: 'PubMed' },
];

// ─── helper ─────────────────────────────────────────────────────────────────

function SectionInfo({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-muted-foreground mb-4">{children}</p>;
}

// ─── main component ──────────────────────────────────────────────────────────

export function ResearchFeedPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const rawSurface = searchParams.get('surface');
  const rawFilter = searchParams.get('filter');

  // M16: unknown surface → 'inbox' fallback
  const surface: SurfaceView =
    rawSurface && VALID_SURFACES.has(rawSurface as SurfaceView)
      ? (rawSurface as SurfaceView)
      : 'inbox';
  // Unknown filter (when surface=library) → no filter (show all library papers)
  const filter: LibraryFilter | null =
    rawFilter && VALID_FILTERS.has(rawFilter as LibraryFilter)
      ? (rawFilter as LibraryFilter)
      : null;

  // Clear bulk selection on any surface change — handles URL-driven changes
  // (browser back/forward, programmatic setSearchParams, deep-links) that
  // imperative click handlers can't intercept.
  useEffect(() => {
    useBulkSelection.getState().clear();
  }, [surface]);

  // Cheat sheet — moved to global AppShell mount in Wave 7 (B.6).
  // The ? keypress is bound by useFeedKeyboardShortcuts in FeedView, which
  // dispatches to useKeyboardShortcuts.getState().open(). The TopBar icon
  // button on every page also opens it.

  const { data: counts } = useFeedCounts();

  // ── default-landing redirect ───────────────────────────────────────────────
  // Ref prevents a second redirect if counts changes identity after the first
  // redirect fires (e.g. React Query background refetch returns a new object).
  const hasRedirectedRef = useRef(false);
  useEffect(() => {
    if (hasRedirectedRef.current) return;
    if (!searchParams.get('surface') && counts) {
      hasRedirectedRef.current = true;
      const target = (counts.inbox ?? 0) > 0 ? 'inbox' : 'library';
      setSearchParams({ surface: target }, { replace: true });
    }
  }, [counts, searchParams, setSearchParams]);

  // ── legacy ?tab=pulse redirect → /my-day ───────────────────────────────────
  useEffect(() => {
    if (searchParams.get('tab') === 'pulse') {
      navigate('/my-day', { replace: true });
    }
  }, [searchParams, navigate]);

  // ── search/save state (preserved from original) ───────────────────────────
  const [previewResults, setPreviewResults] = useState<SearchPreviewResult[]>([]);
  const [sourceErrors, setSourceErrors] = useState<Record<string, SearchPreviewSourceError>>({});
  const [selectedSourceTypes, setSelectedSourceTypes] = useState<string[]>([]);
  const queryClient = useQueryClient();

  const { data: allSources } = useQuery<SourceConfig[]>({
    queryKey: ['sources'],
    queryFn: fetchSources,
  });

  const externalSources = useMemo(
    () => (allSources ?? []).filter((s) => s.source_type !== 'local' && s.enabled),
    [allSources],
  );

  useEffect(() => {
    if (externalSources.length > 0 && selectedSourceTypes.length === 0) {
      setSelectedSourceTypes(externalSources.map((s) => s.source_type));
    }
  }, [externalSources]);

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
          if (!paperId) return paper;
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

  const handleSearch = useCallback(
    (query: string, sourceTypes: string[], maxResults: number, filters: SearchFilters) => {
      searchMutation.mutate({ query, sourceTypes, maxResults, filters });
    },
    [searchMutation],
  );

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

  // ── navigation helpers ────────────────────────────────────────────────────

  function setSurface(s: SurfaceView) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('surface', s);
      next.delete('filter');
      return next;
    });
  }

  function setLibraryFilter(f: LibraryFilter | undefined) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (f === undefined) {
        next.delete('filter');
      } else {
        next.set('filter', f);
      }
      return next;
    });
  }

  const inboxSource = (searchParams.get('source') ?? undefined) as InboxSourceFilter | undefined;

  function setInboxSource(s: InboxSourceFilter | undefined) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (s === undefined) next.delete('source');
      else next.set('source', s);
      return next;
    });
  }

  const feedCounts = counts;

  // ─── render ───────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <h1 className="flex items-center gap-2 text-[32px] leading-tight tracking-tight text-strong">
        <BookOpenIcon className="h-8 w-8" />
        Research Feed
      </h1>
      <p className="text-muted-foreground text-sm">
        Discover and manage research papers from your configured sources
      </p>

      {/* ── Surface chips ─────────────────────────────────────────────────── */}
      <div className="border-b border-hair flex flex-wrap gap-2" role="tablist" aria-label="Surface navigation">
        {SURFACES.map(({ value, label, countsKey }) => (
          <button
            key={value}
            role="tab"
            aria-selected={surface === value}
            onClick={() => setSurface(value)}
            className={cn(
              'inline-flex h-9 -mb-px items-center gap-2 px-4 text-sm font-medium border-b-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
              surface === value
                ? 'border-[hsl(var(--ring))] text-strong'
                : 'border-transparent text-muted-foreground hover:text-strong',
            )}
          >
            {label}
            {countsKey && feedCounts?.[countsKey] !== undefined && (
              <CountsBadge surface={countsKey} />
            )}
          </button>
        ))}
      </div>

      {/* ── Library sub-chips (spec §5.4 — 5 items: All + 4 filters) ────── */}
      {surface === 'library' && (
        <TooltipProvider delayDuration={300}>
          <div className="border-b border-hair flex flex-wrap gap-2" role="tablist" aria-label="Library filter">
            {LIBRARY_SUB_CHIPS.map(({ value, label, icon, tooltip }) => {
              const chipButton = (
                <button
                  role="tab"
                  aria-selected={filter === (value ?? null)}
                  onClick={() => setLibraryFilter(value)}
                  className={cn(
                    'inline-flex h-8 -mb-px items-center gap-1.5 px-3 text-xs font-medium border-b-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
                    filter === (value ?? null)
                      ? 'border-[hsl(var(--ring))] text-strong'
                      : 'border-transparent text-muted-foreground hover:text-strong',
                  )}
                >
                  {icon}
                  {label}
                </button>
              );
              return (
                <div key={value ?? '__all__'}>
                  {tooltip ? (
                    <Tooltip>
                      <TooltipTrigger asChild>{chipButton}</TooltipTrigger>
                      <TooltipContent side="bottom">{tooltip}</TooltipContent>
                    </Tooltip>
                  ) : (
                    chipButton
                  )}
                </div>
              );
            })}
          </div>
        </TooltipProvider>
      )}

      {/* ── Inbox source-type sub-chips ─────────────────────────────────── */}
      {surface === 'inbox' && (
        <div className="border-b border-hair flex flex-wrap gap-2" role="tablist" aria-label="Filter by source">
          {INBOX_SOURCE_CHIPS.map(({ value, label }) => (
            <button
              key={value ?? 'all'}
              role="tab"
              aria-selected={(inboxSource ?? undefined) === value}
              onClick={() => setInboxSource(value)}
              className={cn(
                'inline-flex h-8 -mb-px items-center gap-1.5 px-3 text-xs font-medium border-b-2 transition-colors',
                (inboxSource ?? undefined) === value
                  ? 'border-[hsl(var(--ring))] text-strong'
                  : 'border-transparent text-muted-foreground hover:text-strong',
              )}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {/* ── Surface content ───────────────────────────────────────────────── */}

      {/* Inbox */}
      {surface === 'inbox' && (
        <div>
          <SectionInfo>Unread papers from your configured sources — mark as read, view, or filter.</SectionInfo>
          <FeedView surface="inbox" filter={filter} sourceTypes={inboxSource ?? null} />
        </div>
      )}

      {/* Library */}
      {surface === 'library' && (
        <div>
          <SectionInfo>Browse, search, and filter all papers in your library.</SectionInfo>
          <FeedView surface="library" filter={filter} />
        </div>
      )}

      {/* Trash */}
      {surface === 'trash' && (
        <div>
          <SectionInfo>Papers you have archived or removed from your active library.</SectionInfo>
          <div
            className="rounded border p-3 text-sm mb-3"
            style={{
              borderColor: 'hsl(var(--cta-warn-border))',
              backgroundColor: 'hsl(var(--cta-warn-bg))',
              color: 'hsl(var(--cta-warn-fg))',
            }}
            role="alert"
          >
            Papers in Trash will be kept until you delete them forever. Restore returns them to their previous location.
          </div>
          <FeedView surface="trash" filter={filter} />
        </div>
      )}

      {/* Search */}
      {surface === 'search' && (
        <div className="space-y-4">
          <SectionInfo>Search external databases live and save new papers to your library.</SectionInfo>
          <div>
            <p className="text-xs text-muted-foreground mb-2">
              Search across your enabled sources — results can be added to your library.
            </p>
          </div>

          {externalSources.length > 0 && (
            <div className="space-y-1">
              <div className="flex flex-wrap gap-x-4 gap-y-2 items-center">
                <span className="text-xs font-medium text-muted-foreground">Sources:</span>
                {externalSources.map((source) => (
                  <label
                    key={source.source_type}
                    className="flex items-center gap-1.5 cursor-pointer select-none"
                  >
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
                    <span className="text-sm">
                      {SOURCE_LABELS[source.source_type] ?? source.source_type}
                    </span>
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
          {searchErrorMessage && (
            <p className="text-sm text-destructive">{searchErrorMessage}</p>
          )}
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
      )}

      {/* Ask */}
      {surface === 'ask' && (
        <div className="flex flex-col">
          <SectionInfo>Ask AI questions answered from your indexed paper library.</SectionInfo>
          <div className="mb-3">
            <p className="text-xs text-muted-foreground">
              Get answers synthesised from your entire library.
            </p>
          </div>
          <div className="min-h-[400px]">
            <StreamingChat chatId="cross-paper-rag" scope="cross-paper" />
          </div>
        </div>
      )}
    </div>
  );
}

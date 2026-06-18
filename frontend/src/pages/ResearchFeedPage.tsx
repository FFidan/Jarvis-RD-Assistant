import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  searchPreview,
  batchSavePapers,
  fetchSources,
  fetchFeedCounts,
} from '@/lib/api';
import { useOnlineStatus } from '@/hooks/use-online-status';
import { getPersistedCacheTimestamp } from '@/lib/query-persister';
import { OfflineIndicator } from '@/components/shared/OfflineIndicator';
import type { SearchFilters } from '@/lib/api';
import type {
  SearchPreviewResult,
  SearchPreviewSourceError,
  SourceConfig,
  SurfaceView,
  LibraryFilter,
  InboxSourceFilter,
  FeedScope,
  FeedCountsWithFacets,
} from '@/types';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { errorMessage } from '@/lib/errors';
import { SearchBar } from '@/components/feed/SearchBar';
import { PreviewResults } from '@/components/feed/PreviewResults';
import { SearchSourceErrors } from '@/components/feed/SearchSourceErrors';
import { SOURCE_LABELS } from '@/components/feed/source-labels';
import { FeedView } from '@/components/feed/FeedView';
import { FacetRail } from '@/components/feed/FacetRail';
import type { FacetSelection } from '@/components/feed/FacetRail';
import { FeedListFilter } from '@/components/feed/FeedListFilter';
import { PdfUploadZone } from '@/components/feed/PdfUploadZone';
import { useBulkSelection } from '@/stores/bulk-selection-store';
import { BookOpen as BookOpenIcon, Upload, Compass } from 'lucide-react';

// ─── URL-param helpers ───────────────────────────────────────────────────────

// 4 top-level surfaces: Ask is now its own nav destination (F4), not a feed tab.
const VALID_SURFACES: ReadonlySet<SurfaceView> = new Set<SurfaceView>([
  'inbox',
  'library',
  'search',
  'trash',
]);

const VALID_FILTERS: ReadonlySet<LibraryFilter> = new Set<LibraryFilter>([
  'starred',
  'reading',
  'to_read',
  'done',
]);


// ─── surface definitions (kept for CountsBadge usage) ───────────────────────


const FEED_SCOPES: { value: FeedScope; label: string }[] = [
  { value: 'library', label: 'My library' },
  { value: 'corpus', label: 'All discovered' },
];

// ─── helper ─────────────────────────────────────────────────────────────────

function SectionInfo({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-muted-foreground mb-4">{children}</p>;
}

// ─── main component ──────────────────────────────────────────────────────────

export function ResearchFeedPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { online } = useOnlineStatus();

  // P1b: fetch the cache timestamp once so the Library header can show
  // "stale-cached · as of T" when offline.
  const [cacheTimestamp, setCacheTimestamp] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    getPersistedCacheTimestamp().then((ts) => {
      if (!cancelled) setCacheTimestamp(ts);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const rawSurface = searchParams.get('surface');
  const rawFilter = searchParams.get('filter');
  const rawScope = searchParams.get('scope');
  const rawSource = searchParams.get('source');
  const rawFacetSource = searchParams.get('facet_source');
  const rawFacetTopic = searchParams.get('facet_topic');
  // ?q= carries a prefill query from the command palette; ?action=upload scrolls to the upload zone.
  const rawQ = searchParams.get('q') ?? '';
  const actionUpload = searchParams.get('action') === 'upload';

  // M16: unknown surface → 'inbox' fallback (spec §3.5: default = Inbox)
  // 'ask' is removed from feed; any ?surface=ask URL lands on inbox.
  const surface: SurfaceView =
    rawSurface && VALID_SURFACES.has(rawSurface as SurfaceView)
      ? (rawSurface as SurfaceView)
      : 'inbox';

  const filter: LibraryFilter | null =
    rawFilter && VALID_FILTERS.has(rawFilter as LibraryFilter)
      ? (rawFilter as LibraryFilter)
      : null;

  const feedScope: FeedScope = rawScope === 'corpus' ? 'corpus' : 'library';

  // Parse inbox source filter — no type assertions needed since we verify membership first
  const inboxSource: InboxSourceFilter | null =
    rawSource !== null && (rawSource === 'arxiv' || rawSource === 'semantic_scholar' || rawSource === 'openalex' || rawSource === 'pubmed')
      ? rawSource
      : null;

  const sourceFacet: string | null = rawFacetSource ?? null;

  const rawTopicVal = rawFacetTopic;
  const topicFacet: number | 'untagged' | null =
    rawTopicVal === 'untagged'
      ? 'untagged'
      : rawTopicVal && !isNaN(Number(rawTopicVal))
        ? Number(rawTopicVal)
        : null;

  // Ref for the PDF upload zone — used to scroll+focus when ?action=upload is set.
  const uploadZoneRef = useRef<HTMLDivElement>(null);

  // Scoped list-filter: title/author text filter within the active faceted view
  const [listFilter, setListFilter] = useState('');

  // Clear bulk selection + list filter on surface/filter/facet change
  useEffect(() => {
    useBulkSelection.getState().clear();
    setListFilter('');
  }, [surface, feedScope, filter, sourceFacet, topicFacet]);

  // ── feed counts (numeric only — CountsBadge consumers) ───────────────────
  const { data: counts } = useQuery<FeedCountsWithFacets>({
    queryKey: QUERY_KEYS.feed.counts(),
    queryFn: () => fetchFeedCounts(),
    staleTime: 5_000,
  });

  // ── feed counts with facets (§-facet rail) — scoped to active feedScope ──
  // C-FACET-BE: backend get_feed_counts accepts ?scope= and honours it for
  // by_source / by_topic / untagged facet counts via fetch_feed_facet_counts.
  const { data: countsWithFacets } = useQuery<FeedCountsWithFacets>({
    queryKey: QUERY_KEYS.feed.counts(feedScope),
    queryFn: () => fetchFeedCounts(feedScope),
    staleTime: 5_000,
  });

  // ── default-landing redirect — spec §3.5 + offline contract ─────────────
  // Online: default → Inbox. Offline: default → Library (cached read surface).
  // Feed spec §3.5/offline-contract: "offline, fall back to Library".
  const hasRedirectedRef = useRef(false);
  useEffect(() => {
    if (hasRedirectedRef.current) return;
    if (!searchParams.get('surface')) {
      // Offline: redirect immediately without waiting for counts (which won't
      // arrive without a network).
      if (!online) {
        hasRedirectedRef.current = true;
        setSearchParams({ surface: 'library' }, { replace: true });
        return;
      }
      // Online: wait for counts then redirect to Inbox.
      if (counts) {
        hasRedirectedRef.current = true;
        setSearchParams({ surface: 'inbox' }, { replace: true });
      }
    }
  }, [counts, online, searchParams, setSearchParams]);

  // ── Scroll to upload zone when ?action=upload is present ────────────────
  useEffect(() => {
    if (surface === 'search' && actionUpload && uploadZoneRef.current) {
      uploadZoneRef.current.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    }
  }, [surface, actionUpload]);

  // ── legacy ?tab=pulse redirect → /my-day ─────────────────────────────────
  const navigate = useNavigate();
  useEffect(() => {
    if (searchParams.get('tab') === 'pulse') {
      navigate('/my-day', { replace: true });
    }
  }, [searchParams, navigate]);
  // NOTE: We do NOT handle ?surface=ask here — it is silently redirected to
  // inbox above. The Ask route is a separate nav destination (/ask).

  // ── search/save state (preserved from original) ──────────────────────────
  const [previewResults, setPreviewResults] = useState<SearchPreviewResult[]>([]);
  const [sourceErrors, setSourceErrors] = useState<Record<string, SearchPreviewSourceError>>({});
  const [selectedSourceTypes, setSelectedSourceTypes] = useState<string[]>([]);
  const queryClient = useQueryClient();

  const handleUploadComplete = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.feedAll() });
    void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.feed.counts() });
  }, [queryClient]);

  const { data: allSources } = useQuery<SourceConfig[]>({
    queryKey: QUERY_KEYS.sources.list(),
    queryFn: fetchSources,
  });

  const externalSources = useMemo(
    () => (allSources ?? []).filter((s) => s.source_type !== 'local' && s.enabled),
    [allSources],
  );

  // Initialise selectedSourceTypes once when sources first load.
  // A ref guards against re-initialising after the user manually deselects sources.
  const sourcesInitialisedRef = useRef(false);
  useEffect(() => {
    if (!sourcesInitialisedRef.current && externalSources.length > 0) {
      sourcesInitialisedRef.current = true;
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
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.papers.feedAll() });
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.feed.counts() });
      toast.success(`Saved ${data.length} paper(s). Next: Analyze a paper to unlock Ask.`);
    },
    onError: (error) => {
      toast.error(errorMessage(error, 'Save failed. Check service logs.'));
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
      ? errorMessage(searchMutation.error, 'Search failed. Please try again.')
      : null;

  // ── FacetRail selection ──────────────────────────────────────────────────

  const facetSelection: FacetSelection = {
    surface,
    filter,
    inboxSource,
    sourceFacet,
    topicFacet,
  };

  const handleFacetSelect = useCallback(
    (next: Partial<FacetSelection>) => {
      setSearchParams((prev) => {
        const p = new URLSearchParams(prev);

        if (next.surface !== undefined) {
          p.set('surface', next.surface);
          // Clear filter when switching surfaces
          if (next.surface !== surface) p.delete('filter');
          if (next.surface !== 'library') p.delete('scope');
        }
        if ('filter' in next) {
          if (next.filter === null || next.filter === undefined) {
            p.delete('filter');
          } else {
            p.set('filter', next.filter);
          }
        }
        if ('inboxSource' in next) {
          if (next.inboxSource === null || next.inboxSource === undefined) {
            p.delete('source');
          } else {
            p.set('source', next.inboxSource);
          }
        }
        if ('sourceFacet' in next) {
          if (next.sourceFacet === null || next.sourceFacet === undefined) {
            p.delete('facet_source');
          } else {
            p.set('facet_source', next.sourceFacet);
          }
        }
        if ('topicFacet' in next) {
          if (next.topicFacet === null || next.topicFacet === undefined) {
            p.delete('facet_topic');
          } else {
            p.set('facet_topic', String(next.topicFacet));
          }
        }

        // Always reset pagination on facet change
        p.delete('offset');

        return p;
      });
    },
    [setSearchParams, surface],
  );

  // ── library scope helpers (preserved from original) ──────────────────────

  function setFeedScope(scope: FeedScope) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('surface', 'library');
      if (scope === 'library') next.delete('scope');
      else next.set('scope', scope);
      next.delete('offset');
      return next;
    });
    useBulkSelection.getState().clear();
  }

  // ── Compute effective FeedView filter from facet state ───────────────────
  // §Source drives the `sourceTypes` param; the §Topic facet drives `topicId`.
  // The 'untagged' sentinel has no backend topic id, so it drives a separate
  // `untagged` boolean rather than a topic_id filter.
  const effectiveSourceTypes: string | null = sourceFacet ?? inboxSource ?? null;
  const effectiveTopicId: number | null = typeof topicFacet === 'number' ? topicFacet : null;
  const effectiveUntagged: boolean = topicFacet === 'untagged';

  // ─── render ───────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Page header */}
      <div className="shrink-0 px-4 pb-3 pt-4 sm:px-6">
        <h1 className="flex items-center gap-2 text-[28px] leading-tight tracking-tight text-strong">
          {surface === 'search' ? (
            <Compass className="h-7 w-7" />
          ) : (
            <BookOpenIcon className="h-7 w-7" />
          )}
          {surface === 'search' ? 'Discover' : 'Library'}
        </h1>
      </div>

      {/* ── 3-pane layout ──────────────────────────────────────────────── */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left: §-facet rail — passes isOnline + feedScope for scope-honest copy */}
        <FacetRail
          counts={countsWithFacets}
          selection={facetSelection}
          onSelect={handleFacetSelect}
          isOnline={online}
          feedScope={feedScope}
        />

        {/* Right: main list pane */}
        <main className="flex min-w-0 flex-1 flex-col overflow-y-auto px-4 py-3 sm:px-6">
          {/* Scoped list-filter (spec §3.4 — NOT intent-routing) */}
          {(surface === 'inbox' || surface === 'library' || surface === 'trash') && (
            <div className="mb-3 flex items-center gap-3">
              <FeedListFilter
                value={listFilter}
                onChange={setListFilter}
                className="min-w-0 flex-1"
                placeholder={
                  surface === 'inbox'
                    ? 'Search inbox by title, author, or abstract…'
                    : surface === 'library'
                      ? 'Search library by title, author, or abstract…'
                      : 'Filter trash by title or author…'
                }
              />

              {/* Upload PDF button — visible on Inbox and Library surfaces */}
              {(surface === 'inbox' || surface === 'library') && (
                <button
                  type="button"
                  data-testid="upload-pdf-button"
                  onClick={() =>
                    setSearchParams((prev) => {
                      const p = new URLSearchParams(prev);
                      p.set('surface', 'search');
                      p.set('action', 'upload');
                      return p;
                    })
                  }
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-hair bg-muted/40 px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Upload size={14} aria-hidden />
                  Upload PDF
                </button>
              )}

              {/* Library scope toggle (preserved) */}
              {surface === 'library' && (
                <div
                  className="ml-auto inline-flex rounded-md border border-hair p-0.5"
                  role="tablist"
                  aria-label="Library corpus scope"
                >
                  {FEED_SCOPES.map(({ value, label }) => (
                    <button
                      key={value}
                      role="tab"
                      aria-selected={feedScope === value}
                      onClick={() => setFeedScope(value)}
                      className={cn(
                        'h-7 rounded px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                        feedScope === value
                          ? 'bg-muted text-strong'
                          : 'text-muted-foreground hover:text-strong',
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ── Surface content ─────────────────────────────────────────── */}

          {/* Inbox — online-only surface (live triage feed). When offline the
              default redirect above sends users to Library instead, but a user
              may still manually navigate here via URL or breadcrumb.        */}
          {surface === 'inbox' && (
            <div>
              <SectionInfo>
                Unread papers from your configured sources — mark as read, view, or filter.
              </SectionInfo>
              {!online && (
                <div
                  className="mb-3 flex items-center gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200"
                  data-testid="inbox-offline-notice"
                  role="status"
                >
                  <OfflineIndicator variant="online-only" label="Inbox" />
                  <span className="ml-1">Inbox requires connectivity — switch to Library to read cached papers.</span>
                </div>
              )}
              <FeedView
                surface="inbox"
                filter={filter}
                sourceTypes={effectiveSourceTypes}
                topicId={effectiveTopicId}
                untagged={effectiveUntagged}
                listFilter={listFilter || undefined}
              />
            </div>
          )}

          {/* Library — offline-capable read surface. Shows stale-cached indicator
              when offline and timestamp is known, otherwise "available offline". */}
          {surface === 'library' && (
            <div>
              <div className="mb-1 flex items-center gap-2">
                <SectionInfo>
                  {feedScope === 'corpus'
                    ? 'All discovered papers — the shared global corpus across all sources.'
                    : filter === 'reading'
                      ? 'Papers you\'re currently reading.'
                      : filter === 'to_read'
                        ? 'Papers saved to read later.'
                        : filter === 'done'
                          ? 'Papers you\'ve finished.'
                          : 'My library — papers you\'ve saved or own.'}
                </SectionInfo>
                {!online && (
                  <span className="ml-1 shrink-0" data-testid="library-offline-indicator">
                    {cacheTimestamp != null ? (
                      <OfflineIndicator variant="stale-cached" timestamp={cacheTimestamp} />
                    ) : (
                      <OfflineIndicator variant="available-offline" />
                    )}
                  </span>
                )}
              </div>

              {/* Empty-library default: guide fresh users to Discover instead of a dead list */}
              {counts && counts.library === 0 && feedScope === 'library' && !filter ? (
                <div
                  className="flex flex-col items-center gap-4 rounded-lg border border-dashed border-hair bg-muted/20 px-6 py-10 text-center"
                  data-testid="library-empty-discover"
                >
                  <Compass className="h-10 w-10 text-muted-foreground/50" aria-hidden />
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-foreground">Your library is empty</p>
                    <p className="text-sm text-muted-foreground">
                      Discover papers or upload a PDF to start building your library.
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      data-testid="empty-library-discover-btn"
                      onClick={() =>
                        setSearchParams((prev) => {
                          const p = new URLSearchParams(prev);
                          p.set('surface', 'search');
                          return p;
                        })
                      }
                      className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <Compass size={14} aria-hidden />
                      Discover papers
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        setSearchParams((prev) => {
                          const p = new URLSearchParams(prev);
                          p.set('surface', 'search');
                          p.set('action', 'upload');
                          return p;
                        })
                      }
                      className="inline-flex items-center gap-1.5 rounded-md border border-hair bg-muted/40 px-4 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <Upload size={14} aria-hidden />
                      Upload PDF
                    </button>
                  </div>
                </div>
              ) : (
                <FeedView
                  surface="library"
                  filter={filter}
                  scope={feedScope}
                  sourceTypes={effectiveSourceTypes}
                  topicId={effectiveTopicId}
                  untagged={effectiveUntagged}
                  listFilter={listFilter || undefined}
                />
              )}
            </div>
          )}

          {/* Trash — now a §Status facet, not a top-level tab */}
          {surface === 'trash' && (
            <div>
              <SectionInfo>
                Papers you have archived or removed from your active library.
              </SectionInfo>
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
              <FeedView surface="trash" filter={filter} sourceTypes={effectiveSourceTypes} topicId={effectiveTopicId} untagged={effectiveUntagged} listFilter={listFilter || undefined} />
            </div>
          )}

          {/* Search / Discover — online-only surface (live external DB search).
              When offline: show disabled state with explanatory indicator.     */}
          {surface === 'search' && (
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <SectionInfo>
                  Search external databases live and save new papers to your library.
                </SectionInfo>
                {!online && <OfflineIndicator variant="online-only" label="Search" />}
              </div>

              {!online ? (
                /* Offline: disabled overlay explaining the surface is unavailable */
                <div
                  className="rounded-md border border-hair bg-muted/30 px-4 py-6 text-center text-sm text-muted-foreground"
                  data-testid="search-offline-notice"
                >
                  <p className="font-medium mb-1">Search unavailable offline</p>
                  <p>Searching external databases requires an internet connection.</p>
                </div>
              ) : (
                <>
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

                  {/* When ?action=upload the upload zone is hoisted above search to be immediately visible. */}
                  {actionUpload && (
                    <div ref={uploadZoneRef} className="border-b border-hair pb-4 mb-2" data-testid="upload-zone-hoisted">
                      <p className="mb-2 text-xs font-medium text-muted-foreground">
                        Upload a local PDF:
                      </p>
                      <PdfUploadZone onComplete={handleUploadComplete} />
                    </div>
                  )}

                  <SearchBar
                    onSearch={handleSearch}
                    isLoading={searchMutation.isPending}
                    sourceTypes={selectedSourceTypes}
                    initialQuery={rawQ}
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

                  {/* PDF upload zone — always available at bottom; hoisted to top when ?action=upload */}
                  {!actionUpload && (
                    <div className="mt-6 border-t border-hair pt-4">
                      <p className="mb-2 text-xs font-medium text-muted-foreground">
                        Or upload a local PDF:
                      </p>
                      <PdfUploadZone onComplete={handleUploadComplete} />
                    </div>
                  )}
                </>
              )}
            </div>
          )}

        </main>
      </div>

    </div>
  );
}

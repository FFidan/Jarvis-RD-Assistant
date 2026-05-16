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
import type { SurfaceView, LibraryFilter, InboxSourceFilter, FeedScope } from '@/types';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
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
import { useFeedCountsWithFacets } from '@/hooks/use-feed-counts-with-facets';
import { BookOpen as BookOpenIcon } from 'lucide-react';

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

  const rawSurface = searchParams.get('surface');
  const rawFilter = searchParams.get('filter');
  const rawScope = searchParams.get('scope');
  const rawSource = searchParams.get('source');
  const rawFacetSource = searchParams.get('facet_source');
  const rawFacetTopic = searchParams.get('facet_topic');

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

  // Scoped list-filter: title/author text filter within the active faceted view
  const [listFilter, setListFilter] = useState('');

  // Clear bulk selection + list filter on surface change
  useEffect(() => {
    useBulkSelection.getState().clear();
    setListFilter('');
  }, [surface, feedScope]);

  // ── feed counts (numeric only — CountsBadge consumers) ───────────────────
  const { data: counts } = useFeedCounts();

  // ── feed counts with facets (§-facet rail) ───────────────────────────────
  const { data: countsWithFacets } = useFeedCountsWithFacets();

  // ── default-landing redirect — spec §3.5: always Inbox ───────────────────
  const hasRedirectedRef = useRef(false);
  useEffect(() => {
    if (hasRedirectedRef.current) return;
    if (!searchParams.get('surface') && counts) {
      hasRedirectedRef.current = true;
      // Default: Inbox first (reversed from old library-first redirect)
      setSearchParams({ surface: 'inbox' }, { replace: true });
    }
  }, [counts, searchParams, setSearchParams]);

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
      void queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
      void queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
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
  // §Source and §Topic facets are passed as extra query params to FeedView
  // via sourceTypes/topicId props if the FeedView API supports them.
  // For now, §Source drives the `sourceTypes` param (existing API).
  const effectiveSourceTypes: string | null = sourceFacet ?? inboxSource ?? null;

  // ─── render ───────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Page header */}
      <div className="shrink-0 px-4 pb-3 pt-4 sm:px-6">
        <h1 className="flex items-center gap-2 text-[28px] leading-tight tracking-tight text-strong">
          <BookOpenIcon className="h-7 w-7" />
          Research Feed
        </h1>
      </div>

      {/* ── 3-pane layout ──────────────────────────────────────────────── */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left: §-facet rail */}
        <FacetRail
          counts={countsWithFacets}
          selection={facetSelection}
          onSelect={handleFacetSelect}
        />

        {/* Right: main list pane */}
        <main className="flex min-w-0 flex-1 flex-col overflow-y-auto px-4 py-3 sm:px-6">
          {/* Scoped list-filter (spec §3.4 — NOT intent-routing) */}
          {(surface === 'inbox' || surface === 'library' || surface === 'trash') && (
            <div className="mb-3 flex items-center gap-3">
              <FeedListFilter
                value={listFilter}
                onChange={setListFilter}
                className="max-w-sm"
                placeholder={
                  surface === 'inbox'
                    ? 'Filter inbox by title or author…'
                    : surface === 'library'
                      ? 'Filter library by title or author…'
                      : 'Filter trash by title or author…'
                }
              />

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

          {/* Inbox */}
          {surface === 'inbox' && (
            <div>
              <SectionInfo>
                Unread papers from your configured sources — mark as read, view, or filter.
              </SectionInfo>
              <FeedView
                surface="inbox"
                filter={filter}
                sourceTypes={effectiveSourceTypes}
                listFilter={listFilter || undefined}
              />
            </div>
          )}

          {/* Library */}
          {surface === 'library' && (
            <div>
              <SectionInfo>Browse, search, and filter all papers in your library.</SectionInfo>
              <FeedView
                surface="library"
                filter={filter}
                scope={feedScope}
                listFilter={listFilter || undefined}
              />
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
              <FeedView surface="trash" filter={filter} listFilter={listFilter || undefined} />
            </div>
          )}

          {/* Search / Discover — kept as a surface, accessed via Discover in rail */}
          {surface === 'search' && (
            <div className="space-y-4">
              <SectionInfo>
                Search external databases live and save new papers to your library.
              </SectionInfo>
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

              {/* PDF upload preserved in search surface */}
              <div className="mt-6 border-t border-hair pt-4">
                <p className="mb-2 text-xs font-medium text-muted-foreground">
                  Or upload a local PDF:
                </p>
                <PdfUploadZone onComplete={() => {
                  void queryClient.invalidateQueries({ queryKey: ['papers-feed'] });
                  void queryClient.invalidateQueries({ queryKey: ['feed-counts'] });
                }} />
              </div>
            </div>
          )}

        </main>
      </div>

    </div>
  );
}

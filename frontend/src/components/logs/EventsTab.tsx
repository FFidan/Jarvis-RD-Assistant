import { useState, useMemo, useRef, useEffect, Fragment } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { listEvents, getLogsSources } from '@/lib/logs';
import { EventDetail } from './EventDetail';
import { CorrelationGroup } from './CorrelationGroup';
import { ErrorSparkLine } from './ErrorSparkLine';
import { buildPresets } from './presets';
import { LEVEL_BADGE_CLASSES, CATEGORY_BADGE_CLASSES } from './utils';
import { cn } from '@/lib/utils';
import { useSearchParams } from 'react-router-dom';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Search } from 'lucide-react';
import { useUIStore } from '@/stores/ui-store';

const ALL_LEVELS = ['debug', 'info', 'warning', 'error', 'critical'] as const;
const ALL_CATEGORIES = ['error', 'job', 'source', 'auth', 'config', 'infra'] as const;

function Chip({
  label,
  active,
  onClick,
  className,
  tooltip,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  className?: string;
  tooltip?: string;
}) {
  return (
    <button
      onClick={onClick}
      title={tooltip}
      className={cn(
        'rounded-full px-2.5 py-0.5 text-xs font-medium border transition-all',
        active
          ? cn(className, 'border-foreground/30 ring-1 ring-foreground/30')
          : cn(className, 'border-transparent opacity-50 hover:opacity-80'),
      )}
    >
      {label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// EventsTab
// ---------------------------------------------------------------------------

export function EventsTab() {
  const [searchParams] = useSearchParams();

  // Sync initial state from URL params (used by HeaderPill navigation)
  const [levelFilter, setLevelFilter] = useState<string>(
    searchParams.get('level') ?? '',
  );
  const [categoryFilter, setCategoryFilter] = useState<string>('');
  const [sourceFilter, setSourceFilter] = useState<string>('');
  const [since, setSince] = useState<string>(() => {
    const sp = searchParams.get('since');
    if (sp === '24h') {
      return new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    }
    return sp ?? '';
  });
  const [until, setUntil] = useState<string>('');
  const [query, setQuery] = useState<string>('');

  // Free-text search (client-side filter; does NOT change backend call)
  const [searchText, setSearchText] = useState<string>('');

  // Group-by-correlation toggle
  const [groupByCorrelation, setGroupByCorrelation] = useState<boolean>(false);

  // Expand state (flat view)
  const [expandedId, setExpandedId] = useState<number | null>(null);

  // Preset persistence via ui-store
  const logsPreset = useUIStore((s) => s.logsPreset);
  const setLogsPreset = useUIStore((s) => s.setLogsPreset);

  const presets = useMemo(() => buildPresets(), []);

  function applyPreset(id: string) {
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    setLevelFilter(p.level);
    setCategoryFilter(p.category);
    setSourceFilter(p.source);
    setSince(p.since);
    setUntil(p.until);
    setQuery(p.query);
    setLogsPreset(id);
  }

  function clearPreset() {
    setLogsPreset('');
  }

  // Re-apply the persisted preset's filters on mount so that a returning user
  // sees both the preset NAME *and* the corresponding filter state (LOGS-PRESET-RESTORE-NO-FILTERS).
  const didApplyInitialPreset = useRef(false);
  useEffect(() => {
    if (didApplyInitialPreset.current) return;
    didApplyInitialPreset.current = true;
    if (logsPreset) applyPreset(logsPreset);
    // Run once on mount. applyPreset/logsPreset are stable for this purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const { data: sources } = useQuery({
    queryKey: QUERY_KEYS.logs.sources(),
    queryFn: getLogsSources,
    staleTime: 60_000,
  });

  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading,
  } = useInfiniteQuery({
    queryKey: QUERY_KEYS.logs.events(levelFilter, categoryFilter, sourceFilter, since, until, query),
    queryFn: ({ pageParam }) =>
      listEvents({
        level: levelFilter || undefined,
        category: categoryFilter || undefined,
        source: sourceFilter || undefined,
        since: since || undefined,
        until: until || undefined,
        cursor: typeof pageParam === 'number' ? pageParam : undefined,
        limit: 50,
        q: query || undefined,
      }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.next_cursor ?? undefined,
  });

  const allEvents = useMemo(
    () => data?.pages.flatMap((p) => p.events) ?? [],
    [data?.pages],
  );

  // Client-side free-text filter applied on top of backend results
  const filteredEvents = useMemo(() => {
    if (!searchText) return allEvents;
    const lower = searchText.toLowerCase();
    return allEvents.filter((ev) =>
      ev.message.toLowerCase().includes(lower),
    );
  }, [allEvents, searchText]);

  // Group events by correlation_id for progressive-disclosure view.
  // Events without a correlation_id form their own single-item implicit group.
  const correlationGroups = useMemo(() => {
    if (!groupByCorrelation) return null;

    const grouped = new Map<string, typeof allEvents>();
    for (const ev of filteredEvents) {
      const key = ev.correlation_id ?? `__solo__${ev.id}`;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key)!.push(ev);
    }
    return Array.from(grouped.entries());
  }, [filteredEvents, groupByCorrelation]);

  function toggleLevel(lv: string) {
    setLevelFilter((prev) => (prev === lv ? '' : lv));
    clearPreset();
  }

  function toggleCategory(cat: string) {
    setCategoryFilter((prev) => (prev === cat ? '' : cat));
    clearPreset();
  }

  // Honest empty state: distinguish "nothing recorded yet" from "your
  // filters exclude everything" so a user isn't left guessing why the list
  // is blank.
  const hasActiveFilter = Boolean(
    levelFilter || categoryFilter || sourceFilter || since || until || query || searchText,
  );
  const emptyMessage = hasActiveFilter
    ? 'No events match the current filters.'
    : 'No events recorded recently. Events are recorded for auth, jobs, sources, config, and errors.';

  return (
    <div className="space-y-4">
      {/* Spark-line error chart */}
      <ErrorSparkLine events={allEvents} />

      {/* Preset selector */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Preset:</span>
        <select
          data-testid="preset-select"
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
          value={logsPreset ?? ''}
          onChange={(e) => {
            if (e.target.value) {
              applyPreset(e.target.value);
            } else {
              clearPreset();
            }
          }}
        >
          <option value="">— none —</option>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>

        {/* Group-by-correlation toggle */}
        <button
          data-testid="group-toggle"
          onClick={() => setGroupByCorrelation((v) => !v)}
          className={cn(
            'rounded-md border px-3 py-1.5 text-xs font-medium transition-all',
            groupByCorrelation
              ? 'border-foreground/30 bg-muted ring-1 ring-foreground/20'
              : 'border-input bg-background opacity-70 hover:opacity-100',
          )}
        >
          Group by correlation
        </button>
      </div>

      {/* Filters */}
      <div className="space-y-3">
        {/* Severity chips */}
        <div className="flex flex-wrap gap-1.5 items-center">
          <span className="text-xs text-muted-foreground mr-1">Severity:</span>
          {ALL_LEVELS.map((lv) => (
            <Chip
              key={lv}
              label={lv}
              active={levelFilter === lv}
              onClick={() => toggleLevel(lv)}
              className={LEVEL_BADGE_CLASSES[lv]}
            />
          ))}
          {levelFilter && (
            <button
              type="button"
              aria-label="Clear severity filter"
              onClick={() => { setLevelFilter(''); clearPreset(); }}
              className="text-xs text-muted-foreground underline hover:text-foreground"
            >
              Clear
            </button>
          )}
        </div>

        {/* Area chips */}
        <div className="flex flex-wrap gap-1.5 items-center">
          <span className="text-xs text-muted-foreground mr-1">Area:</span>
          {ALL_CATEGORIES.map((cat) => (
            <Chip
              key={cat}
              label={cat}
              active={categoryFilter === cat}
              onClick={() => toggleCategory(cat)}
              className={CATEGORY_BADGE_CLASSES[cat]}
              tooltip={cat === 'infra' ? 'Infrastructure events forwarded by the log pipeline; may be empty if not configured' : undefined}
            />
          ))}
          {categoryFilter && (
            <button
              type="button"
              aria-label="Clear area filter"
              onClick={() => { setCategoryFilter(''); clearPreset(); }}
              className="text-xs text-muted-foreground underline hover:text-foreground"
            >
              Clear
            </button>
          )}
        </div>

        {/* Source select + date range + backend search + client free-text */}
        <div className="flex flex-wrap gap-2">
          <select
            className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
            value={sourceFilter}
            onChange={(e) => { setSourceFilter(e.target.value); clearPreset(); }}
          >
            <option value="">All sources</option>
            {(sources ?? []).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>

          <div className="flex flex-col gap-0.5">
            <Label htmlFor="events-since-input" className="text-xs text-muted-foreground">
              Since
            </Label>
            <input
              id="events-since-input"
              type="datetime-local"
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
              value={since ? since.slice(0, 16) : ''}
              onChange={(e) => {
                setSince(e.target.value ? new Date(e.target.value).toISOString() : '');
                clearPreset();
              }}
              placeholder="Since"
              title="Since"
            />
          </div>
          <div className="flex flex-col gap-0.5">
            <Label htmlFor="events-until-input" className="text-xs text-muted-foreground">
              Until
            </Label>
            <input
              id="events-until-input"
              type="datetime-local"
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
              value={until ? until.slice(0, 16) : ''}
              onChange={(e) => {
                setUntil(e.target.value ? new Date(e.target.value).toISOString() : '');
                clearPreset();
              }}
              placeholder="Until"
              title="Until"
            />
          </div>

          {/* Backend full-text search */}
          <div className="flex flex-col gap-0.5 w-full sm:w-auto sm:min-w-[160px]">
            <Label htmlFor="events-search-all-input" className="text-xs text-muted-foreground">
              Search all events
            </Label>
            <div className="relative">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                id="events-search-all-input"
                className="pl-8"
                placeholder="Full-text search…"
                value={query}
                onChange={(e) => { setQuery(e.target.value); clearPreset(); }}
              />
            </div>
          </div>

          {/* Client-side free-text search */}
          <div className="flex flex-col gap-0.5 w-full sm:w-auto sm:min-w-[160px]">
            <Label htmlFor="events-filter-rows-input" className="text-xs text-muted-foreground">
              Filter loaded rows
            </Label>
            <div className="relative">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                id="events-filter-rows-input"
                data-testid="search-input"
                className="pl-8"
                placeholder="Filter rows…"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
              />
            </div>
          </div>
        </div>
      </div>

      {/* Events list */}
      {isLoading && (
        <p className="text-sm text-muted-foreground">Loading events…</p>
      )}

      {/* Grouped view */}
      {!isLoading && groupByCorrelation && correlationGroups && (
        <>
          {correlationGroups.length === 0 && (
            <p className="text-sm text-muted-foreground">{emptyMessage}</p>
          )}
          <div className="space-y-2">
            {correlationGroups.map(([corrId, evs]) => {
              const isSolo = corrId.startsWith('__solo__');
              if (isSolo) {
                // Single event without correlation_id — render flat row
                const ev = evs[0];
                if (!ev) return null;
                return (
                  <div
                    key={corrId}
                    className="rounded-md border border-border overflow-hidden"
                  >
                    <div className="flex items-start gap-3 px-3 py-2 text-xs">
                      <span className="text-muted-foreground shrink-0 font-mono whitespace-nowrap">
                        {new Date(ev.created_at).toLocaleString()}
                      </span>
                      <span
                        className={cn(
                          'shrink-0 rounded px-1 py-0.5 font-medium',
                          LEVEL_BADGE_CLASSES[ev.level] ?? '',
                        )}
                      >
                        {ev.level}
                      </span>
                      <span
                        className={cn(
                          'shrink-0 rounded px-1 py-0.5',
                          CATEGORY_BADGE_CLASSES[ev.category] ?? '',
                        )}
                      >
                        {ev.category}
                      </span>
                      <span className="text-muted-foreground shrink-0">{ev.source}</span>
                      <span className="break-all flex-1">{ev.message}</span>
                    </div>
                  </div>
                );
              }
              return (
                <CorrelationGroup
                  key={corrId}
                  correlationId={corrId}
                  events={evs}
                  searchText={searchText}
                />
              );
            })}
          </div>
        </>
      )}

      {/* Flat view */}
      {!isLoading && !groupByCorrelation && (
        <>
          {filteredEvents.length === 0 && (
            <p className="text-sm text-muted-foreground">{emptyMessage}</p>
          )}
          {filteredEvents.length > 0 && (
            <div className="rounded-md border border-border overflow-hidden divide-y divide-border">
              {filteredEvents.map((ev) => (
                <Fragment key={ev.id}>
                  <button
                    className="w-full flex items-start gap-3 px-3 py-2 text-xs hover:bg-muted/30 text-left transition-colors"
                    onClick={() => setExpandedId((id) => (id === ev.id ? null : ev.id))}
                    aria-expanded={expandedId === ev.id}
                  >
                    <span className="text-muted-foreground shrink-0 font-mono whitespace-nowrap">
                      {new Date(ev.created_at).toLocaleString()}
                    </span>
                    <span
                      className={cn(
                        'shrink-0 rounded px-1 py-0.5 font-medium',
                        LEVEL_BADGE_CLASSES[ev.level] ?? '',
                      )}
                    >
                      {ev.level}
                    </span>
                    <span
                      className={cn(
                        'shrink-0 rounded px-1 py-0.5',
                        CATEGORY_BADGE_CLASSES[ev.category] ?? '',
                      )}
                    >
                      {ev.category}
                    </span>
                    <span className="text-muted-foreground shrink-0">{ev.source}</span>
                    <span className="break-all flex-1">{ev.message}</span>
                  </button>
                  {expandedId === ev.id && (
                    <div className="px-3 pb-3">
                      <EventDetail event={ev} />
                    </div>
                  )}
                </Fragment>
              ))}
            </div>
          )}
        </>
      )}

      {/* Infinite scroll trigger */}
      {hasNextPage && (
        <button
          onClick={() => fetchNextPage()}
          disabled={isFetchingNextPage}
          className="w-full rounded-md border border-dashed border-border py-2 text-sm text-muted-foreground hover:text-foreground hover:border-foreground/30 transition-colors"
        >
          {isFetchingNextPage ? 'Loading more…' : 'Load more events'}
        </button>
      )}
    </div>
  );
}

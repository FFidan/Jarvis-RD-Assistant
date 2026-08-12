import { useMemo, useRef, useState } from 'react';
import { Check, Search } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { cloudProviderLabel } from '@/lib/api';
import type { ModelCatalogEntry, ProviderModelListStatus } from '@/lib/api';
import { cn } from '@/lib/utils';
import {
  filterAndSortModels,
  modelPriceLabel,
  modelSource,
  openRouterUpstream,
  type GenerativeModelRole,
  type ModelPriceFilter,
  type ModelSort,
} from './model-options';

interface ModelPickerDialogProps {
  role: GenerativeModelRole;
  models: ModelCatalogEntry[];
  selectedId: string;
  reviewedIds: ReadonlySet<string>;
  providerLists: Record<string, ProviderModelListStatus>;
  blockerFor: (entry: ModelCatalogEntry) => string | null;
  onSelect: (modelId: string) => void;
  initialSource?: string;
  defaultOpen?: boolean;
}

function sourceLabel(source: string): string {
  return source === 'local' ? 'Local models' : cloudProviderLabel(source);
}

function roleLabel(role: GenerativeModelRole): string {
  return role === 'fast' ? 'Quick' : 'Main';
}

function modelCountLabel(count: number): string {
  return `${count} model${count === 1 ? '' : 's'}`;
}

function formatMetadataDate(value: string | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  }).format(date);
}

function ModelMetadata({ entry }: { entry: ModelCatalogEntry }) {
  const capabilities = entry.capabilities?.map((item) => item.replace(/_/g, ' ')).join(', ');
  const sources = Object.values(entry.field_sources ?? {});
  const apiSource = sources.find((source) => source.kind === 'api_reported');
  const reviewedSource = sources.find((source) => source.kind === 'reviewed_catalog');
  const fetchedAt = formatMetadataDate(apiSource?.fetched_at);
  const reviewedAt = formatMetadataDate(reviewedSource?.reviewed_at);
  const provenance = apiSource
    ? `Provider metadata${fetchedAt ? ` · fetched ${fetchedAt}` : ''}`
    : reviewedSource
      ? `Reviewed metadata${reviewedAt ? ` · reviewed ${reviewedAt}` : ''}`
      : entry.last_reviewed
        ? `Catalog reviewed ${formatMetadataDate(entry.last_reviewed) ?? entry.last_reviewed}`
        : 'Provider catalogue entry';
  const facts = [capabilities ? `Capabilities: ${capabilities}` : null, entry.lifecycle ? `Lifecycle: ${entry.lifecycle}` : null]
    .filter((item): item is string => item != null);

  return (
    <div className="mt-1 space-y-0.5 text-xs text-muted-foreground">
      {facts.length > 0 && <p>{facts.join(' · ')}</p>}
      <p>{provenance}</p>
    </div>
  );
}

const MAX_VISIBLE_MODEL_ROWS = 150;

export function ModelPickerDialog({
  role,
  models,
  selectedId,
  reviewedIds,
  providerLists,
  blockerFor,
  onSelect,
  initialSource,
  defaultOpen = false,
}: ModelPickerDialogProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [query, setQuery] = useState('');
  const [source, setSource] = useState(
    initialSource ?? (reviewedIds.size > 0 ? 'reviewed' : 'local'),
  );
  const [price, setPrice] = useState<ModelPriceFilter>('all');
  const [sort, setSort] = useState<ModelSort>('reviewed');
  const [upstream, setUpstream] = useState('all');
  const searchRef = useRef<HTMLInputElement>(null);

  const sources = useMemo(
    () => Array.from(new Set([...models.map(modelSource), ...Object.keys(providerLists)])).sort((left, right) => {
      if (left === 'local') return right === 'local' ? 0 : -1;
      if (right === 'local') return 1;
      return sourceLabel(left).localeCompare(sourceLabel(right));
    }),
    [models, providerLists],
  );
  const upstreams = useMemo(
    () => Array.from(new Set(models.map(openRouterUpstream).filter((item): item is string => item != null))).sort(),
    [models],
  );
  const visibleModels = useMemo(
    () => filterAndSortModels(models, { query, source, price, sort, upstream, reviewedIds }),
    [models, price, query, reviewedIds, sort, source, upstream],
  );
  const selectedSourceStatus = source === 'reviewed' || source === 'local'
    ? undefined
    : providerLists[source];

  const chooseSource = (nextSource: string) => {
    setSource(nextSource);
    setUpstream('all');
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" size="sm" data-testid={`change-model-${role}`}>
          Change model
        </Button>
      </DialogTrigger>
      <DialogContent
        className="h-[min(88vh,56rem)] w-[min(96vw,90rem)] min-w-0 max-w-none grid-rows-[auto_auto_minmax(0,1fr)] gap-0 overflow-hidden p-0"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          searchRef.current?.focus();
        }}
      >
        <DialogHeader className="border-b border-hair px-6 py-5 pr-14">
          <DialogTitle className="font-serif text-2xl">Choose a {roleLabel(role)} model</DialogTitle>
          <DialogDescription>
            Reviewed choices first. Browse a local or provider catalog only when needed.
          </DialogDescription>
        </DialogHeader>

        <div className="border-b border-hair px-6 py-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="model-picker-search"
              name="model-picker-search"
              ref={searchRef}
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search within the selected source"
              className="pl-9"
              aria-label="Search models"
            />
          </div>
        </div>

        <div className="grid min-h-0 min-w-0 flex-1 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[15rem_minmax(0,1fr)] md:grid-rows-1">
          <nav
            aria-label="Model sources"
            className="flex min-w-0 items-center gap-1 overflow-x-auto border-b border-hair bg-muted/25 p-3 md:block md:overflow-y-auto md:border-b-0 md:border-r"
          >
            <p className="hidden px-2 pb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground md:block">
              Browse
            </p>
            {reviewedIds.size > 0 && (
              <button
                type="button"
                onClick={() => chooseSource('reviewed')}
                className={cn(
                  'flex shrink-0 items-center gap-2 rounded-md px-2 py-2 text-left text-sm md:w-full md:justify-between',
                  source === 'reviewed' ? 'bg-background font-medium shadow-sm' : 'hover:bg-background/70',
                )}
                aria-current={source === 'reviewed' ? 'page' : undefined}
                aria-label={`Reviewed choices, ${modelCountLabel(reviewedIds.size)}`}
              >
                <span>Reviewed choices</span>
                <span className="font-mono text-xs text-muted-foreground">{reviewedIds.size}</span>
              </button>
            )}
            {sources.map((item) => {
              const count = models.filter((entry) => modelSource(entry) === item).length;
              return (
                <button
                  key={item}
                  type="button"
                  onClick={() => chooseSource(item)}
                  className={cn(
                    'flex shrink-0 items-center gap-2 rounded-md px-2 py-2 text-left text-sm md:mt-1 md:w-full md:justify-between',
                    source === item ? 'bg-background font-medium shadow-sm' : 'hover:bg-background/70',
                  )}
                  aria-current={source === item ? 'page' : undefined}
                  aria-label={`${sourceLabel(item)}, ${modelCountLabel(count)}`}
                >
                  <span>{sourceLabel(item)}</span>
                  <span className="font-mono text-xs text-muted-foreground">{count}</span>
                </button>
              );
            })}
          </nav>

          <section className="flex min-h-0 min-w-0 flex-col" aria-label="Available models">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-hair px-4 py-3">
              <div>
                <h3 className="font-semibold">{source === 'reviewed' ? 'Reviewed choices' : sourceLabel(source)}</h3>
                <p className="text-xs text-muted-foreground">
                  {selectedSourceStatus?.error
                    ? 'Live catalog unavailable; built-in entries may still be shown.'
                    : `${visibleModels.length} matching model${visibleModels.length === 1 ? '' : 's'}`}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                {source === 'openrouter' && upstreams.length > 0 && (
                  <label className="sr-only" htmlFor={`model-upstream-${role}`}>Upstream provider</label>
                )}
                {source === 'openrouter' && upstreams.length > 0 && (
                  <select
                    id={`model-upstream-${role}`}
                    value={upstream}
                    onChange={(event) => setUpstream(event.target.value)}
                    className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                  >
                    <option value="all">All upstreams</option>
                    {upstreams.map((item) => <option key={item} value={item}>{item}</option>)}
                  </select>
                )}
                <label className="sr-only" htmlFor={`model-price-${role}`}>Price</label>
                <select
                  id={`model-price-${role}`}
                  value={price}
                  onChange={(event) => {
                    const nextPrice = event.target.value;
                    if (
                      nextPrice === 'all' ||
                      nextPrice === 'free' ||
                      nextPrice === 'paid' ||
                      nextPrice === 'unknown'
                    ) {
                      setPrice(nextPrice);
                    }
                  }}
                  className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                >
                  <option value="all">All prices</option>
                  <option value="free">Free</option>
                  <option value="paid">Paid</option>
                  <option value="unknown">Price unavailable</option>
                </select>
                <label className="sr-only" htmlFor={`model-sort-${role}`}>Sort models</label>
                <select
                  id={`model-sort-${role}`}
                  value={sort}
                  onChange={(event) => {
                    const nextSort = event.target.value;
                    if (
                      nextSort === 'reviewed' ||
                      nextSort === 'name' ||
                      nextSort === 'input-price' ||
                      nextSort === 'output-price'
                    ) {
                      setSort(nextSort);
                    }
                  }}
                  className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                >
                  <option value="reviewed">Reviewed choices first</option>
                  <option value="name">Name</option>
                  <option value="input-price">Lowest input price</option>
                  <option value="output-price">Lowest output price</option>
                </select>
              </div>
            </div>

            <div className="min-h-0 min-w-0 flex-1 overflow-auto">
              {visibleModels.length === 0 ? (
                <p className="p-8 text-center text-sm text-muted-foreground">
                  No models match these filters.
                </p>
              ) : (
                <>
                  <Table className="min-w-[48rem]">
                    <TableHeader className="sticky top-0 z-10 bg-background">
                      <TableRow>
                        <TableHead>Model</TableHead>
                        <TableHead>Provider</TableHead>
                        <TableHead>Context</TableHead>
                        <TableHead>Price</TableHead>
                        <TableHead className="sticky right-0 w-28 bg-background">
                          <span className="sr-only">Choose</span>
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {visibleModels.slice(0, MAX_VISIBLE_MODEL_ROWS).map((entry) => {
                        const blocker = blockerFor(entry);
                        const selected = entry.id === selectedId;
                        const upstreamProvider = openRouterUpstream(entry);
                        return (
                          <TableRow key={entry.id} className="group" data-testid={`model-row-${entry.id}`}>
                            <TableCell>
                              <p className="font-medium">{entry.name}</p>
                              <p className="break-all font-mono text-xs text-muted-foreground">{entry.id}</p>
                              {entry.description && (
                                <p className="mt-1 max-w-xl text-xs text-muted-foreground">{entry.description}</p>
                              )}
                              <ModelMetadata entry={entry} />
                              {blocker && <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">{blocker}</p>}
                            </TableCell>
                            <TableCell>
                              <span>{sourceLabel(modelSource(entry))}</span>
                              {upstreamProvider && <span className="block text-xs text-muted-foreground">through {upstreamProvider}</span>}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {entry.context_tokens > 0 ? entry.context_tokens.toLocaleString() : 'Not reported'}
                            </TableCell>
                            <TableCell className="text-xs">{modelPriceLabel(entry)}</TableCell>
                            <TableCell className="sticky right-0 bg-background group-hover:bg-muted/50">
                              <Button
                                type="button"
                                size="sm"
                                variant={selected ? 'outline' : 'default'}
                                disabled={blocker != null || selected}
                                onClick={() => {
                                  onSelect(entry.id);
                                  setOpen(false);
                                }}
                                aria-label={selected ? `${entry.name} is current` : `Use ${entry.name}`}
                              >
                                {selected && <Check className="mr-1 h-3.5 w-3.5" />}
                                {selected ? 'Current' : 'Use'}
                              </Button>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                  {visibleModels.length > MAX_VISIBLE_MODEL_ROWS && (
                    <p className="p-4 text-center text-sm text-muted-foreground">
                      Showing the first {MAX_VISIBLE_MODEL_ROWS} of {visibleModels.length} models. Refine your search to see more.
                    </p>
                  )}
                </>
              )}
            </div>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}

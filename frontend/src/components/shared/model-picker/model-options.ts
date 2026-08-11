import type { ModelCatalogEntry } from '@/lib/api';

export type GenerativeModelRole = 'fast' | 'smart';
export type ModelPriceFilter = 'all' | 'free' | 'paid' | 'unknown';
export type ModelSort = 'recommended' | 'name' | 'input-price' | 'output-price';

export function isLocalModel(entry: ModelCatalogEntry): boolean {
  return entry.provider === 'ollama' || Boolean(entry.ollama_tag);
}

export function modelSource(entry: ModelCatalogEntry): string {
  return isLocalModel(entry) ? 'local' : entry.provider;
}

export function normalizeModelId(value: string): string {
  return value.replace(/:latest$/, '');
}

export function matchesModelId(entry: ModelCatalogEntry, value: string): boolean {
  if (!value) return false;
  return [entry.id, entry.ollama_tag]
    .filter((candidate): candidate is string => Boolean(candidate))
    .some((candidate) => normalizeModelId(candidate) === normalizeModelId(value));
}

export function openRouterUpstream(entry: ModelCatalogEntry): string | null {
  if (entry.provider !== 'openrouter') return null;
  const parts = entry.id.split('/');
  if (parts[0] === 'openrouter') return parts[1] ?? null;
  return parts.length > 1 ? parts[0] ?? null : null;
}

function numericPrice(value: string | null | undefined): number | null {
  if (value == null || value.trim() === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export function priceKind(entry: ModelCatalogEntry): Exclude<ModelPriceFilter, 'all'> {
  const input = numericPrice(entry.input_price_per_million);
  const output = numericPrice(entry.output_price_per_million);
  if (input == null && output == null) return 'unknown';
  if (input === 0 && output === 0) return 'free';
  return 'paid';
}

export function modelPriceLabel(entry: ModelCatalogEntry): string {
  const input = entry.input_price_per_million;
  const output = entry.output_price_per_million;
  if (priceKind(entry) === 'free') return 'Free';
  if (input == null && output == null) return 'Price unavailable';
  const inputLabel = input == null ? 'unknown' : `$${input}`;
  const outputLabel = output == null ? 'unknown' : `$${output}`;
  return `${inputLabel} input / ${outputLabel} output per 1M tokens`;
}

function priceForSort(entry: ModelCatalogEntry, field: 'input' | 'output'): number {
  const value = numericPrice(
    field === 'input' ? entry.input_price_per_million : entry.output_price_per_million,
  );
  return value ?? Number.POSITIVE_INFINITY;
}

export interface ModelFilterOptions {
  query: string;
  source: string;
  price: ModelPriceFilter;
  upstream: string;
  sort: ModelSort;
  recommendedIds: ReadonlySet<string>;
}

export function filterAndSortModels(
  models: readonly ModelCatalogEntry[],
  options: ModelFilterOptions,
): ModelCatalogEntry[] {
  const query = options.query.trim().toLocaleLowerCase();
  const filtered = models.filter((entry) => {
    if (options.source === 'recommended' && !options.recommendedIds.has(entry.id)) return false;
    if (
      options.source !== 'recommended' &&
      options.source !== 'all' &&
      modelSource(entry) !== options.source
    ) {
      return false;
    }
    if (options.price !== 'all' && priceKind(entry) !== options.price) return false;
    if (
      options.upstream !== 'all' &&
      openRouterUpstream(entry)?.toLocaleLowerCase() !== options.upstream.toLocaleLowerCase()
    ) {
      return false;
    }
    if (!query) return true;
    return [entry.name, entry.id, entry.provider, entry.description]
      .join(' ')
      .toLocaleLowerCase()
      .includes(query);
  });

  return filtered.sort((left, right) => {
    if (options.sort === 'recommended') {
      const rank = Number(options.recommendedIds.has(right.id)) - Number(options.recommendedIds.has(left.id));
      if (rank !== 0) return rank;
    }
    if (options.sort === 'input-price' || options.sort === 'output-price') {
      const field = options.sort === 'input-price' ? 'input' : 'output';
      const priceOrder = priceForSort(left, field) - priceForSort(right, field);
      if (priceOrder !== 0 && Number.isFinite(priceOrder)) return priceOrder;
      if (priceForSort(left, field) !== priceForSort(right, field)) {
        return priceForSort(left, field) === Number.POSITIVE_INFINITY ? 1 : -1;
      }
    }
    return left.name.localeCompare(right.name) || left.id.localeCompare(right.id);
  });
}

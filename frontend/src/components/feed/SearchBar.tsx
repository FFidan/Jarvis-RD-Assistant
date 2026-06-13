import { useState } from 'react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Search, Loader2, ChevronDown, ChevronUp, SlidersHorizontal } from 'lucide-react';
import type { SearchFilters } from '@/lib/api';
import { Badge } from '@/components/ui/badge';

// Note: Select is retained for the filters panel (Sort By, Max Results)

interface SearchBarProps {
  onSearch: (query: string, sourceTypes: string[], maxResults: number, filters: SearchFilters) => void;
  isLoading: boolean;
  /** Currently selected source types (controlled externally). */
  sourceTypes: string[];
  /** Prefill the search input (e.g. query carried from the command palette). */
  initialQuery?: string;
}

const DEFAULT_FILTERS: SearchFilters = {
  yearFrom: undefined,
  yearTo: undefined,
  sortBy: 'relevance',
  author: undefined,
};

function countActiveFilters(filters: SearchFilters): number {
  let count = 0;
  if (filters.yearFrom !== undefined) count++;
  if (filters.yearTo !== undefined) count++;
  if (filters.sortBy && filters.sortBy !== 'relevance') count++;
  if (filters.author && filters.author.trim()) count++;
  return count;
}

export function SearchBar({ onSearch, isLoading, sourceTypes, initialQuery = '' }: SearchBarProps) {
  const [query, setQuery] = useState(initialQuery);
  const [maxResults, setMaxResults] = useState(10);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [filters, setFilters] = useState<SearchFilters>(DEFAULT_FILTERS);

  function handleSubmit() {
    const trimmed = query.trim();
    if (!trimmed) return;
    onSearch(trimmed, sourceTypes, maxResults, filters);
  }

  function handleClearFilters() {
    setFilters(DEFAULT_FILTERS);
  }

  function setFilter<K extends keyof SearchFilters>(key: K, value: SearchFilters[K]) {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }

  const activeFilterCount = countActiveFilters(filters);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input
          placeholder="Search your selected sources…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleSubmit();
          }}
          className="flex-1"
        />
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setFiltersOpen((o) => !o)}
            className="flex items-center gap-1 relative"
            type="button"
            aria-expanded={filtersOpen}
            aria-label="Toggle filters"
          >
            <SlidersHorizontal className="h-4 w-4" />
            Filters
            {activeFilterCount > 0 && (
              <Badge variant="secondary" className="ml-1 px-1.5 py-0 text-xs h-5">
                {activeFilterCount}
              </Badge>
            )}
            {filtersOpen ? (
              <ChevronUp className="h-3 w-3 ml-1" />
            ) : (
              <ChevronDown className="h-3 w-3 ml-1" />
            )}
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={isLoading || !query.trim() || sourceTypes.length === 0}
          >
            {isLoading ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Search className="mr-2 h-4 w-4" />
            )}
            Search
          </Button>
        </div>
      </div>

      {filtersOpen && (
        <div className="rounded-lg border bg-muted/30 p-3 space-y-3">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-muted-foreground">Year From</label>
              <Input
                type="number"
                min={1900}
                max={2100}
                placeholder="e.g. 2020"
                value={filters.yearFrom ?? ''}
                onChange={(e) =>
                  setFilter('yearFrom', e.target.value ? Number(e.target.value) : undefined)
                }
                className="h-8 text-sm"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-muted-foreground">Year To</label>
              <Input
                type="number"
                min={1900}
                max={2100}
                placeholder="e.g. 2024"
                value={filters.yearTo ?? ''}
                onChange={(e) =>
                  setFilter('yearTo', e.target.value ? Number(e.target.value) : undefined)
                }
                className="h-8 text-sm"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-muted-foreground">Sort By</label>
              <Select
                value={filters.sortBy ?? 'relevance'}
                onValueChange={(v) => setFilter('sortBy', v as 'relevance' | 'date')}
              >
                <SelectTrigger className="h-8 text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="relevance">Relevance</SelectItem>
                  <SelectItem value="date">Date (newest first)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-muted-foreground">Max Results</label>
              <Select
                value={String(maxResults)}
                onValueChange={(v) => setMaxResults(Number(v))}
              >
                <SelectTrigger className="h-8 text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="10">10</SelectItem>
                  <SelectItem value="25">25</SelectItem>
                  <SelectItem value="50">50</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex items-end gap-3">
            <div className="flex flex-col gap-1 flex-1">
              <label className="text-xs font-medium text-muted-foreground">Author</label>
              <Input
                type="text"
                placeholder="Author name"
                value={filters.author ?? ''}
                onChange={(e) => setFilter('author', e.target.value || undefined)}
                className="h-8 text-sm"
              />
            </div>
            {activeFilterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleClearFilters}
                type="button"
                className="h-8 text-xs"
              >
                Clear filters
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

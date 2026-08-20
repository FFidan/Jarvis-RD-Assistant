import { useState, useEffect, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import type { SearchPreviewResult, SearchPreviewSourceError } from '@/types';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Save, X, Loader2 } from 'lucide-react';
import { SearchPreviewDrawer } from '@/components/feed/SearchPreviewDrawer';
import { SearchPreviewRow } from '@/components/feed/SearchPreviewRow';
import { SOURCE_LABELS } from '@/components/feed/source-labels';

interface PreviewResultsProps {
  papers: SearchPreviewResult[];
  onSave: (papers: SearchPreviewResult[]) => void;
  onClear: () => void;
  isSaving: boolean;
  perSourceCounts?: Record<string, number>;
  sourceErrors?: Record<string, SearchPreviewSourceError>;
}

export function PreviewResults({
  papers,
  onSave,
  onClear,
  isSaving,
  perSourceCounts = {},
  sourceErrors = {},
}: PreviewResultsProps) {
  const navigate = useNavigate();
  const saveablePapers = useMemo(
    () => papers.filter((paper) => !paper.library_match?.paper_id),
    [papers],
  );
  const saveableIds = useMemo(
    () => new Set(saveablePapers.map((paper) => paper.external_id)),
    [saveablePapers],
  );
  // Nothing is pre-selected: search results are candidates the researcher opts
  // INTO, and pre-selecting everything next to "Save all unsaved" made a stray
  // click bulk-import an unreviewed set.
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [sortField, setSortField] = useState<'relevance' | 'date' | 'title' | 'citations'>('relevance');
  const [previewPaper, setPreviewPaper] = useState<SearchPreviewResult | null>(null);
  const previousPaperIdsRef = useRef<string[] | null>(null);

  const sortedPapers = useMemo(() => {
    const copy = [...papers];
    switch (sortField) {
      case 'date':
        return copy.sort((a, b) => (b.published_date ?? '').localeCompare(a.published_date ?? ''));
      case 'title':
        return copy.sort((a, b) => (a.title ?? '').localeCompare(b.title ?? ''));
      case 'citations':
        return copy.sort((a, b) => (b.citation_count ?? 0) - (a.citation_count ?? 0));
      default:
        return copy;
    }
  }, [papers, sortField]);

  // Preserve partial selection across in-place save reconciliation. A fresh
  // result set starts empty, and an update to the same result set only removes
  // rows that became non-saveable.
  useEffect(() => {
    const nextPaperIds = papers.map((paper) => paper.external_id);
    const previousPaperIds = previousPaperIdsRef.current;
    const nextSaveableIds = new Set(saveablePapers.map((paper) => paper.external_id));
    const sameResultSet =
      previousPaperIds !== null &&
      previousPaperIds.length === nextPaperIds.length &&
      previousPaperIds.every((id) => nextPaperIds.includes(id));

    if (!sameResultSet) {
      setSelected(new Set());
    } else {
      setSelected((current) => {
        const next = new Set<string>();
        current.forEach((id) => {
          if (nextSaveableIds.has(id)) {
            next.add(id);
          }
        });
        return next;
      });
    }

    previousPaperIdsRef.current = nextPaperIds;
  }, [papers, saveablePapers]);

  // Keep the preview drawer anchored to the current result set so stale paper
  // objects do not survive a new search.
  useEffect(() => {
    setPreviewPaper((current) => {
      if (!current) return null;
      return papers.find((paper) => paper.external_id === current.external_id) ?? null;
    });
  }, [papers]);

  function toggleSelection(id: string) {
    if (!saveableIds.has(id)) {
      return;
    }
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function openPaper(paper: SearchPreviewResult) {
    if (paper.library_match?.paper_id) {
      navigate(`/paper/${paper.library_match.paper_id}`);
      return;
    }
    setPreviewPaper(paper);
  }

  const selectedPapers = saveablePapers.filter((paper) => selected.has(paper.external_id));
  const selectedCount = selectedPapers.length;
  const hasSaveablePapers = saveablePapers.length > 0;
  const hasLibraryMatches = saveablePapers.length !== papers.length;
  const sourceTypes = Array.from(
    new Set([...Object.keys(perSourceCounts), ...Object.keys(sourceErrors)]),
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold">
            Results &mdash; {papers.length} found
          </h2>
          <Select value={sortField} onValueChange={(v) => setSortField(v as 'relevance' | 'date' | 'title' | 'citations')}>
            <SelectTrigger className="w-[140px] h-8 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="relevance">Relevance</SelectItem>
              <SelectItem value="date">Newest</SelectItem>
              <SelectItem value="title">Title</SelectItem>
              <SelectItem value="citations">Most Cited</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <p className="text-sm text-muted-foreground">
          Select papers to add to your library, then click Save.
        </p>
      </div>

      <p className="text-sm text-muted-foreground mb-2">{papers.length} results</p>
      {sourceTypes.length > 0 && (
        <ul className="divide-y divide-hair rounded-md border border-hair text-sm">
          {sourceTypes.map((sourceType) => {
            const count = perSourceCounts[sourceType] ?? 0;
            const error = sourceErrors[sourceType];
            return (
              <li
                key={sourceType}
                data-testid={`source-summary-${sourceType}`}
                className="flex flex-wrap items-baseline gap-x-2 gap-y-1 px-3 py-2"
              >
                <span className="font-medium">
                  {SOURCE_LABELS[sourceType] ?? sourceType}
                </span>
                <span className="text-muted-foreground">
                  {/* A source that failed returned nothing because it was never
                      searched — "0 results" would claim it looked and found none. */}
                  {error && count === 0
                    ? 'not searched'
                    : `${count} ${count === 1 ? 'result' : 'results'}`}
                </span>
                {error && (
                  <span className="basis-full text-xs text-muted-foreground">
                    {error.message}
                  </span>
                )}
                {error && (error.status_code !== null || error.retry_after_s !== null) && (
                  <span className="basis-full text-xs text-muted-foreground">
                    {error.status_code !== null && `Status ${error.status_code}`}
                    {error.status_code !== null && error.retry_after_s !== null ? ', ' : ''}
                    {error.retry_after_s !== null && `Retry after ${error.retry_after_s}s`}
                  </span>
                )}
                {error?.settings_hint && !error.message.includes(error.settings_hint) && (
                  <span className="basis-full text-xs text-muted-foreground">
                    {error.settings_hint}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {papers.length > 0 && (!hasSaveablePapers || hasLibraryMatches) && (
        <p className="text-sm text-muted-foreground">
          {hasLibraryMatches && hasSaveablePapers
            ? 'Library-matched results are already in your library and excluded from save actions.'
            : 'All results in this preview are already in your library.'}
        </p>
      )}

      <div className="space-y-2">
        {sortedPapers.map((paper, i) => {
          const isMatched = Boolean(paper.library_match?.paper_id);

          return (
            <SearchPreviewRow
              key={paper.external_id || `paper-${i}`}
              paper={paper}
              selected={selected.has(paper.external_id)}
              isSelectedDisabled={isMatched}
              onToggleSelection={() => toggleSelection(paper.external_id)}
              onPrimaryClick={() => openPaper(paper)}
              onSavePaper={() => onSave([paper])}
              isSaving={isSaving}
            />
          );
        })}
      </div>

      <div className="flex items-center gap-2">
        <Button
          onClick={() => {
            onSave(selectedPapers);
          }}
          disabled={selectedCount === 0 || isSaving}
        >
          {isSaving ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Save className="mr-2 h-4 w-4" />
          )}
          Save {selectedCount} selected
        </Button>
        <Button
          variant="outline"
          onClick={() => onSave(saveablePapers)}
          disabled={!hasSaveablePapers || isSaving}
        >
          Save all unsaved
        </Button>
        <Button variant="ghost" onClick={onClear}>
          <X className="mr-2 h-4 w-4" />
          Clear
        </Button>
      </div>
      {hasLibraryMatches && !hasSaveablePapers && (
        <p className="text-xs text-muted-foreground">
          Nothing here is saveable because every result already matches your library.
        </p>
      )}

      <Separator />

      <SearchPreviewDrawer
        paper={previewPaper}
        open={previewPaper !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPreviewPaper(null);
          }
        }}
        onSave={onSave}
        isSaving={isSaving}
      />
    </div>
  );
}

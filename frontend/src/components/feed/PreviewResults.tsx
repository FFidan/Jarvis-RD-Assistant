import { useState, useEffect, useMemo } from 'react';
import type { SearchPreviewResult } from '@/types';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { formatAuthors, formatDate } from '@/lib/utils';
import { Save, X, Loader2 } from 'lucide-react';

interface PreviewResultsProps {
  papers: SearchPreviewResult[];
  onSave: (papers: SearchPreviewResult[]) => void;
  onClear: () => void;
  isSaving: boolean;
}

export function PreviewResults({ papers, onSave, onClear, isSaving }: PreviewResultsProps) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(papers.map((p) => p.external_id)));
  const [sortField, setSortField] = useState<'relevance' | 'date' | 'title' | 'citations'>('relevance');

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

  // Reset selection when papers change
  useEffect(() => {
    setSelected(new Set(papers.map((p) => p.external_id)));
  }, [papers]);

  function toggleSelection(id: string) {
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

  const selectedCount = selected.size;

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

      <div className="space-y-2">
        {sortedPapers.map((paper, i) => (
          <div
            key={paper.external_id || `paper-${i}`}
            className="flex items-start gap-3 rounded-lg border p-3 hover:bg-accent/50"
          >
            <input
              type="checkbox"
              checked={selected.has(paper.external_id)}
              onChange={() => toggleSelection(paper.external_id)}
              className="mt-1 h-4 w-4 rounded border-gray-300"
              aria-label={`Select ${paper.title || 'paper'}`}
            />
            <div className="min-w-0 flex-1">
              <p className="font-medium leading-tight">{paper.title || 'Untitled'}</p>
              <p className="text-sm text-muted-foreground">
                {formatAuthors(paper.authors)} &middot; {formatDate(paper.published_date)}
              </p>
              {paper.abstract && (
                <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                  {paper.abstract}
                </p>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <Button
          onClick={() => {
            const toSave = papers.filter((p) => selected.has(p.external_id));
            onSave(toSave);
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
          onClick={() => onSave(papers)}
          disabled={isSaving}
        >
          Save all
        </Button>
        <Button variant="ghost" onClick={onClear}>
          <X className="mr-2 h-4 w-4" />
          Clear
        </Button>
      </div>

      <Separator />
    </div>
  );
}

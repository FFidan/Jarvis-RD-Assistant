import { useState, useMemo } from 'react';
import { Input } from '@/components/ui/input';
import { ScrollArea } from '@/components/ui/scroll-area';
import type { PaperBrief } from '@/types';

interface PaperMultiSelectProps {
  papers: PaperBrief[];
  selected: number[];
  onChange: (ids: number[]) => void;
  maxSelections?: number;
}

export function PaperMultiSelect({
  papers,
  selected,
  onChange,
  maxSelections = 20,
}: PaperMultiSelectProps) {
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    if (!search.trim()) return papers;
    const lower = search.toLowerCase();
    return papers.filter((p) => p.title.toLowerCase().includes(lower));
  }, [papers, search]);

  const toggle = (id: number) => {
    if (selected.includes(id)) {
      onChange(selected.filter((s) => s !== id));
    } else if (selected.length < maxSelections) {
      onChange([...selected, id]);
    }
  };

  const selectAll = () => {
    const ids = filtered.slice(0, maxSelections).map((p) => p.id);
    onChange(ids);
  };

  const clearAll = () => onChange([]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label className="text-sm font-medium">
          Select Papers ({selected.length}/{maxSelections})
        </label>
        <div className="flex gap-2">
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={selectAll}
          >
            Select all
          </button>
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={clearAll}
          >
            Clear
          </button>
        </div>
      </div>
      <Input
        placeholder="Search papers..."
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <ScrollArea className="h-48 rounded border p-2">
        {filtered.length === 0 && (
          <p className="py-4 text-center text-sm text-muted-foreground">No papers found</p>
        )}
        {filtered.map((paper) => (
          <label
            key={paper.id}
            className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-accent"
          >
            <input
              type="checkbox"
              checked={selected.includes(paper.id)}
              onChange={() => toggle(paper.id)}
              className="h-4 w-4 rounded border-gray-300"
            />
            <span className="line-clamp-1">
              {paper.title.length > 80 ? `${paper.title.slice(0, 80)}...` : paper.title}
            </span>
          </label>
        ))}
      </ScrollArea>
    </div>
  );
}

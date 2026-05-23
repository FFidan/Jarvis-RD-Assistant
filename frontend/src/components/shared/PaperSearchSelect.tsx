import { useState, useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Search, X, FileText } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { searchPapersBrief, fetchPapersBrief } from '@/lib/api';
import type { PaperBrief } from '@/types';

interface PaperSearchSelectProps {
  value?: number | null;
  values?: number[];
  onChange?: (paperId: number | null) => void;
  onChangeMulti?: (paperIds: number[]) => void;
  placeholder?: string;
}

export function PaperSearchSelect({
  value,
  values,
  onChange,
  onChangeMulti,
  placeholder = 'Search papers by title...',
}: PaperSearchSelectProps) {
  const [search, setSearch] = useState('');
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const isMulti = values !== undefined;

  const [debouncedSearch, setDebouncedSearch] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const { data: papers = [] } = useQuery({
    queryKey: QUERY_KEYS.papersBrief.list(debouncedSearch),
    queryFn: () =>
      debouncedSearch.length >= 2
        ? searchPapersBrief(debouncedSearch)
        : fetchPapersBrief(),
  });

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const selectedPaper = !isMulti && value
    ? papers.find((p) => p.id === value)
    : null;

  const handleSelect = (paper: PaperBrief) => {
    if (isMulti && onChangeMulti) {
      const current = values || [];
      if (current.includes(paper.id)) {
        onChangeMulti(current.filter((id) => id !== paper.id));
      } else {
        onChangeMulti([...current, paper.id]);
      }
    } else if (onChange) {
      onChange(paper.id);
      setSearch('');
      setOpen(false);
    }
  };

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          value={selectedPaper && !open ? selectedPaper.title : search}
          onChange={(e) => { setSearch(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          placeholder={placeholder}
          className="pl-8"
        />
        {value && !isMulti && (
          <Button
            variant="ghost" size="sm"
            className="absolute right-1 top-1 h-6 w-6 p-0"
            onClick={() => { onChange?.(null); setSearch(''); }}
          >
            <X className="h-3 w-3" />
          </Button>
        )}
      </div>

      {isMulti && values && values.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {values.map((id) => {
            const p = papers.find((pp) => pp.id === id);
            return (
              <Badge key={id} variant="secondary" className="gap-1">
                <FileText className="h-3 w-3" />
                {p?.title?.slice(0, 40) || `Paper ${id}`}
                <button onClick={() => onChangeMulti?.(values.filter((v) => v !== id))}>
                  <X className="h-3 w-3" />
                </button>
              </Badge>
            );
          })}
        </div>
      )}

      {open && (
        <div className="absolute z-50 mt-1 max-h-60 w-full overflow-auto rounded-md border bg-popover p-1 shadow-md">
          {papers.length === 0 ? (
            <p className="px-2 py-4 text-center text-sm text-muted-foreground">
              No papers found
            </p>
          ) : (
            papers.slice(0, 50).map((paper) => {
              const isSelected = isMulti
                ? values?.includes(paper.id)
                : value === paper.id;
              return (
                <button
                  key={paper.id}
                  className={`flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent ${
                    isSelected ? 'bg-accent' : ''
                  }`}
                  onClick={() => handleSelect(paper)}
                >
                  <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="line-clamp-2">{paper.title}</span>
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

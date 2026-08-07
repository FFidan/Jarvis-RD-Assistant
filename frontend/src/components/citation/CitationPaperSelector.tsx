import { useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchPapersBrief } from '@/lib/api';
import { PaperSearchSelect } from '@/components/shared/PaperSearchSelect';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import type { PaperBrief } from '@/types';

interface CitationPaperSelectorProps {
  selectedPapers: PaperBrief[];
  onSelectionChange: (papers: PaperBrief[]) => void;
  maxSelections?: number;
}

export function CitationPaperSelector({
  selectedPapers,
  onSelectionChange,
  maxSelections: _maxSelections = 10,
}: CitationPaperSelectorProps) {
  const selectedIds = selectedPapers.map((p) => p.id);

  // Keep a cache of papers for resolving ids back to PaperBrief objects
  const { data: allPapers = [], isError } = useQuery({
    queryKey: QUERY_KEYS.papersBrief.list(),
    queryFn: fetchPapersBrief,
  });

  const handleChangeMulti = useCallback(
    (paperIds: number[]) => {
      // Resolve ids to PaperBrief objects using cached data or existing selection
      const resolved = paperIds.map((id) => {
        const existing = selectedPapers.find((p) => p.id === id);
        if (existing) return existing;
        const fromCache = allPapers.find((p) => p.id === id);
        if (fromCache) return { id: fromCache.id, title: fromCache.title };
        return { id, title: `Paper ${id}` };
      });
      onSelectionChange(resolved);
    },
    [selectedPapers, allPapers, onSelectionChange],
  );

  return (
    <div className="space-y-3">
      <label className="mb-1 block text-sm font-medium">Select Papers to Visualize</label>
      <PaperSearchSelect
        values={selectedIds}
        onChangeMulti={handleChangeMulti}
        placeholder="Search papers to add to citation graph..."
      />
      {isError && <QueryErrorState message="Failed to load papers." />}
      <p className="text-xs text-muted-foreground">
        {selectedPapers.length}/{_maxSelections} papers selected
      </p>
    </div>
  );
}

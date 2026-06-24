import { useState } from 'react';
import { errorMessage } from '@/lib/errors';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchCitationsFromS2 } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Loader2, Download } from 'lucide-react';

interface FetchCitationsButtonProps {
  /** Papers the user has selected; citations are fetched only for these. */
  paperIds: number[];
}

export function FetchCitationsButton({ paperIds }: FetchCitationsButtonProps) {
  const queryClient = useQueryClient();
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async (ids: number[]) => {
      let citationsAdded = 0;
      let referencesAdded = 0;
      for (const id of ids) {
        const res = await fetchCitationsFromS2(id);
        citationsAdded += res.citations_added;
        referencesAdded += res.references_added;
      }
      return { papers: ids.length, citationsAdded, referencesAdded };
    },
    onSuccess: (data) => {
      setStatusMessage(
        `Fetched citations for ${data.papers} paper${data.papers === 1 ? '' : 's'} ` +
          `(${data.citationsAdded} citations, ${data.referencesAdded} references)`,
      );
      // Note: bare prefix for invalidation — no registry factory for citation all-entries
      queryClient.invalidateQueries({ queryKey: ['citation-graph'] });
    },
  });

  return (
    <div className="space-y-2">
      <Button
        onClick={() => mutation.mutate(paperIds)}
        disabled={mutation.isPending || paperIds.length === 0}
        size="sm"
      >
        {mutation.isPending ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <Download className="mr-2 h-4 w-4" />
        )}
        Fetch Citations
      </Button>

      {statusMessage && (
        <p className="text-xs [color:var(--status-ok)]">{statusMessage}</p>
      )}

      {mutation.isError && (
        <p className="text-xs text-destructive">
          Failed to fetch citations: {errorMessage(mutation.error)}
        </p>
      )}
    </div>
  );
}

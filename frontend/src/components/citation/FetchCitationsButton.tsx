import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchCitationsFromS2 } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Loader2, Download } from 'lucide-react';

interface FetchCitationsButtonProps {
  /** Papers the user has selected; citations are fetched only for these. */
  paperIds: number[];
}

interface FetchCitationsResult {
  total: number;
  succeeded: number;
  failedIds: number[];
  citationsAdded: number;
  referencesAdded: number;
}

type PaperOutcome =
  | { id: number; ok: true; citationsAdded: number; referencesAdded: number }
  | { id: number; ok: false };

async function fetchCitationsForAll(ids: number[]): Promise<FetchCitationsResult> {
  const outcomes = await Promise.all(
    ids.map(
      (id): Promise<PaperOutcome> =>
        fetchCitationsFromS2(id).then(
          (res) => ({
            id,
            ok: true,
            citationsAdded: res.citations_added,
            referencesAdded: res.references_added,
          }),
          () => ({ id, ok: false }),
        ),
    ),
  );

  const result: FetchCitationsResult = {
    total: ids.length,
    succeeded: 0,
    failedIds: [],
    citationsAdded: 0,
    referencesAdded: 0,
  };

  for (const outcome of outcomes) {
    if (outcome.ok) {
      result.succeeded += 1;
      result.citationsAdded += outcome.citationsAdded;
      result.referencesAdded += outcome.referencesAdded;
    } else {
      result.failedIds.push(outcome.id);
    }
  }

  return result;
}

export function FetchCitationsButton({ paperIds }: FetchCitationsButtonProps) {
  const queryClient = useQueryClient();
  const [result, setResult] = useState<FetchCitationsResult | null>(null);

  const mutation = useMutation({
    mutationFn: fetchCitationsForAll,
    onSuccess: (data) => {
      setResult(data);
      // Note: bare prefix for invalidation — no registry factory for citation all-entries
      if (data.succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ['citation-graph'] });
      }
    },
  });

  const failedCount = result ? result.failedIds.length : 0;

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

      {result && !mutation.isPending && (
        <div className="space-y-1">
          <p
            className={
              failedCount > 0
                ? 'text-xs text-destructive'
                : 'text-xs [color:var(--status-ok)]'
            }
          >
            {result.succeeded} of {result.total} succeeded ({failedCount} failed)
            {result.succeeded > 0 &&
              ` — ${result.citationsAdded} citations, ${result.referencesAdded} references`}
          </p>

          {failedCount > 0 && (
            <Button
              variant="outline"
              size="sm"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate(result.failedIds)}
            >
              Retry {failedCount} failed
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

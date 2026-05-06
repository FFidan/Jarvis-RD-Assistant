import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { batchFetchCitations } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Loader2, Download } from 'lucide-react';

export function FetchCitationsButton() {
  const queryClient = useQueryClient();
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => batchFetchCitations(),
    onSuccess: (data) => {
      setStatusMessage(data.message);
      queryClient.invalidateQueries({ queryKey: ['citation-graph'] });
    },
  });

  return (
    <div className="space-y-2">
      <Button
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
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
          Failed to fetch citations: {(mutation.error as Error).message}
        </p>
      )}
    </div>
  );
}

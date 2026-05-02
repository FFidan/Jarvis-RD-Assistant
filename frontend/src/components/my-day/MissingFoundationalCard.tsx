import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, Plus } from 'lucide-react';
import {
  fetchAndProcessFoundationalPaper,
  fetchMissingFoundationalPapers,
} from '@/lib/api';
import { useJobStore } from '@/stores/job-store';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import type { MissingFoundationalPaper } from '@/types';

export function MissingFoundationalCard() {
  const queryClient = useQueryClient();
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);

  const { data = [], isLoading, isError } = useQuery({
    queryKey: ['analytics', 'missing-foundational'],
    queryFn: fetchMissingFoundationalPapers,
  });

  const addMut = useMutation({
    mutationFn: (paperId: number) => fetchAndProcessFoundationalPaper(paperId),
    onSuccess: (result) => {
      if (result.job_id) {
        trackExternalJob({
          jobId: result.job_id,
          kind: result.status === 'queued' ? 'paper.analyze' : 'paper.process',
          payload: { paper_id: result.paper_id },
          status: 'queued',
        });
      }
      queryClient.invalidateQueries({ queryKey: ['analytics', 'missing-foundational'] });
    },
  });

  const pendingPaperId =
    addMut.isPending && typeof addMut.variables === 'number' ? addMut.variables : null;

  // Only render when there are gaps (or on error); hide completely when list is empty
  if (!isLoading && !isError && data.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-lg">Missing Foundational Papers</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading citation gaps...</p>
        ) : isError ? (
          <p className="text-sm text-destructive">Failed to load citation gaps.</p>
        ) : (
          data.map((paper: MissingFoundationalPaper) => (
            <div key={paper.paper_id} className="space-y-2 border-b pb-3 last:border-b-0 last:pb-0">
              <div className="space-y-1">
                <p className="text-sm font-medium leading-snug">{paper.title}</p>
                <p className="text-xs text-muted-foreground">
                  {[
                    paper.authors.slice(0, 2).join(', '),
                    paper.year,
                    `${paper.cited_by_library_count} local citations`,
                    `${paper.citation_count} total citations`,
                  ]
                    .filter(Boolean)
                    .join(' | ')}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => addMut.mutate(paper.paper_id)}
                disabled={addMut.isPending}
              >
                {pendingPaperId === paper.paper_id ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Plus className="mr-2 h-4 w-4" />
                )}
                {paper.pdf_available ? 'Add and Process' : 'Add to Library'}
              </Button>
            </div>
          ))
        )}
        {addMut.isError && (
          <p className="text-sm text-destructive">
            {addMut.error instanceof Error
              ? addMut.error.message
              : 'Failed to add citation stub'}
          </p>
        )}
        {addMut.data?.status === 'no_pdf' && (
          <p className="text-sm text-muted-foreground">{addMut.data.message}</p>
        )}
      </CardContent>
    </Card>
  );
}

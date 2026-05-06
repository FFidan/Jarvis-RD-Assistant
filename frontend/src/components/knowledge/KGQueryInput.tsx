import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { queryKnowledgeGraph } from '@/lib/api';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Loader2, Search } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function KGQueryInput() {
  const [query, setQuery] = useState('');

  const mutation = useMutation({
    mutationFn: (q: string) => queryKnowledgeGraph(q),
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (query.trim()) {
      mutation.mutate(query.trim());
    }
  };

  return (
    <div className="space-y-3">
      <form onSubmit={handleSubmit} className="flex gap-2">
        <Input
          placeholder="Query (e.g., 'What methods are used on ImageNet?')"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="flex-1"
        />
        <Button type="submit" disabled={!query.trim() || mutation.isPending} size="sm">
          {mutation.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Search className="mr-2 h-4 w-4" />
          )}
          Query
        </Button>
      </form>

      {mutation.isError && (
        <p className="text-xs text-destructive">
          Query failed: {(mutation.error as Error).message}
        </p>
      )}

      {mutation.data && mutation.data.results.length > 0 && (
        <Card className="rounded-md border-hair shadow-none">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Query Results</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {mutation.data.results.map((r, i) => (
              <pre
                key={`result-${i}`}
                className="overflow-x-auto rounded bg-muted p-2 text-xs"
              >
                {JSON.stringify(r, null, 2)}
              </pre>
            ))}
          </CardContent>
        </Card>
      )}

      {mutation.data && mutation.data.results.length === 0 && (
        <p className="text-sm text-muted-foreground">No results found for this query.</p>
      )}
    </div>
  );
}

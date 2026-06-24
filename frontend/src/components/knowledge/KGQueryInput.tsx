import { useState } from 'react';
import { errorMessage } from '@/lib/errors';
import { useMutation } from '@tanstack/react-query';
import { queryKnowledgeGraph } from '@/lib/api';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Loader2, Search } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

type QueryResult = Record<string, unknown>;

function asString(v: unknown): string | null {
  if (v == null) return null;
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return null;
}

function confidencePct(v: unknown): string | null {
  if (typeof v !== 'number' || Number.isNaN(v)) return null;
  return `${Math.round(v * 100)}%`;
}

/**
 * Render one heterogeneous KG query result as a structured card.
 *
 * The backend (`query_knowledge_graph`) returns one of three row shapes depending
 * on the query pattern:
 *   - "used on" / "applied to"  → relationship rows with method_name + target_name
 *   - "outperforms" / "better than" → comparison rows with method_name + compared_to
 *   - generic                   → entity rows with name + entity_type
 * Anything else falls back to a labelled key/value list (never raw JSON).
 */
function ResultCard({ result }: { result: QueryResult }) {
  const methodName = asString(result.method_name);
  const targetName = asString(result.target_name);
  const comparedTo = asString(result.compared_to);
  const name = asString(result.name);
  const entityType = asString(result.entity_type);
  const evidence = asString(result.evidence_quote);
  const confidence = confidencePct(result.confidence);

  // Shape A: "used on" / "applied to" relationship
  if (methodName && targetName) {
    const relationship = asString(result.relationship_type) ?? 'related to';
    return (
      <div className="rounded-md border-hair p-3">
        <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="font-medium text-strong">{methodName}</span>
          <span className="text-muted-foreground">{relationship.replace(/_/g, ' ')}</span>
          <span className="font-medium text-strong">{targetName}</span>
          {confidence && (
            <span className="ml-auto text-xs text-muted-foreground">confidence {confidence}</span>
          )}
        </div>
        {evidence && <p className="mt-1 text-xs italic text-muted-foreground">&ldquo;{evidence}&rdquo;</p>}
      </div>
    );
  }

  // Shape B: "outperforms" / "better than" comparison
  if (methodName && comparedTo) {
    return (
      <div className="rounded-md border-hair p-3">
        <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="font-medium text-strong">{methodName}</span>
          <span className="text-muted-foreground">outperforms</span>
          <span className="font-medium text-strong">{comparedTo}</span>
          {confidence && (
            <span className="ml-auto text-xs text-muted-foreground">confidence {confidence}</span>
          )}
        </div>
        {evidence && <p className="mt-1 text-xs italic text-muted-foreground">&ldquo;{evidence}&rdquo;</p>}
      </div>
    );
  }

  // Shape C: generic entity row
  if (name) {
    const description = asString(result.description);
    const paperTitle = asString(result.paper_title);
    return (
      <div className="rounded-md border-hair p-3">
        <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="font-medium text-strong">{name}</span>
          {entityType && (
            <span className="text-xs text-muted-foreground">{entityType.replace(/_/g, ' ')}</span>
          )}
        </div>
        {description && <p className="mt-1 text-xs text-muted-foreground">{description}</p>}
        {paperTitle && <p className="mt-1 text-xs text-muted-foreground">From: {paperTitle}</p>}
      </div>
    );
  }

  // Fallback: unknown shape — still structured (labelled key/value), never raw JSON.
  const entries = Object.entries(result).filter(([, v]) => asString(v) != null);
  if (entries.length === 0) {
    return (
      <div className="rounded-md border-hair p-3 text-xs text-muted-foreground">
        Result has no displayable fields.
      </div>
    );
  }
  return (
    <div className="rounded-md border-hair p-3">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        {entries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="font-medium text-muted-foreground">{k.replace(/_/g, ' ')}</dt>
            <dd className="text-strong">{asString(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

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
          Query failed: {errorMessage(mutation.error)}
        </p>
      )}

      {mutation.data && mutation.data.results.length > 0 && (
        <Card className="rounded-md border-hair shadow-none">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Query Results</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {mutation.data.results.map((r, i) => (
              <ResultCard key={`result-${i}`} result={r} />
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

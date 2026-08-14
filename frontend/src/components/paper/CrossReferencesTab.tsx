import { Link } from 'react-router-dom';
import { useQueries } from '@tanstack/react-query';
import type { Summary, CrossReference } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { Link2 } from 'lucide-react';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchPaperDetail } from '@/lib/api';

const RELATIONSHIP_LABELS: Record<string, string> = {
  semantic_similarity: 'Similar content',
  potential_overlap: 'Possible overlap',
};

const TITLE_STALE_MS = 5 * 60_000;

interface CrossReferencesTabProps {
  summary: Summary | null;
}

/**
 * Resolve the titles of cross-referenced papers, keyed by paper id.
 *
 * The cross-reference payload carries only ids, and a link that does not name
 * its target is unusable. This is the sole place that join happens on the
 * client — when the summary payload starts carrying related titles, only this
 * hook changes.
 */
function useCrossReferenceTitles(refs: CrossReference[]): Map<number, string> {
  const results = useQueries({
    queries: refs.map((ref) => ({
      queryKey: QUERY_KEYS.papers.detail(ref.related_paper_id),
      queryFn: () => fetchPaperDetail(ref.related_paper_id),
      staleTime: TITLE_STALE_MS,
    })),
  });
  return new Map(
    results
      .map((result) => result.data)
      .filter((detail) => detail != null)
      .map((detail) => [detail.paper.id, detail.paper.title] as const),
  );
}

export function CrossReferencesTab({ summary }: CrossReferencesTabProps) {
  const refs: CrossReference[] = summary?.cross_references ?? [];
  // Hooks run before any early return so their order stays render-stable.
  const titleById = useCrossReferenceTitles(refs);

  if (!summary) {
    return (
      <EmptyState
        icon={Link2}
        title="No summary available"
        description="Generate a summary first to see cross-references."
      />
    );
  }

  if (refs.length === 0) {
    return (
      <EmptyState
        icon={Link2}
        title="No cross-references"
        description="No strong cross-references found yet — they appear as more of your library is analyzed."
      />
    );
  }

  return (
    <div className="space-y-3">
      {refs.map((ref) => (
        <Card key={`${ref.related_paper_id}-${ref.relationship}`} className="rounded-md border-hair shadow-none">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">
              <Link to={`/paper/${ref.related_paper_id}`} className="underline hover:no-underline">
                {titleById.get(ref.related_paper_id) ?? 'Open related paper'}
              </Link>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm">
            <p>
              <span className="font-medium">Relationship:</span>{' '}
              {RELATIONSHIP_LABELS[ref.relationship] ?? ref.relationship}
            </p>
            <p className="text-muted-foreground">{ref.explanation}</p>
            {ref.related_quote && (
              <blockquote className="border-l-2 border-muted-foreground/30 pl-3 italic text-muted-foreground">
                &ldquo;{ref.related_quote}&rdquo;
              </blockquote>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

import { Link } from 'react-router-dom';
import type { Summary, CrossReference } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { Link2 } from 'lucide-react';

const RELATIONSHIP_LABELS: Record<string, string> = {
  semantic_similarity: 'Similar content',
  potential_overlap: 'Possible overlap',
};

interface CrossReferencesTabProps {
  summary: Summary | null;
}

export function CrossReferencesTab({ summary }: CrossReferencesTabProps) {
  const refs: CrossReference[] = summary?.cross_references ?? [];

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
              {ref.related_title ? (
                <Link to={`/paper/${ref.related_paper_id}`} className="underline hover:no-underline">
                  {ref.related_title}
                  {ref.related_year ? ` (${ref.related_year})` : ''}
                </Link>
              ) : (
                <span>Related paper unavailable (ID {ref.related_paper_id})</span>
              )}
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

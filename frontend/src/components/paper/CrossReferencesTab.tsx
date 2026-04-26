import type { Summary, CrossReference } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { Link2 } from 'lucide-react';

interface CrossReferencesTabProps {
  summary: Summary | null;
}

export function CrossReferencesTab({ summary }: CrossReferencesTabProps) {
  if (!summary) {
    return (
      <EmptyState
        icon={Link2}
        title="No summary available"
        description="Generate a summary first to see cross-references."
      />
    );
  }

  const refs: CrossReference[] = summary.cross_references ?? [];

  if (refs.length === 0) {
    return (
      <EmptyState
        icon={Link2}
        title="No cross-references"
        description="No cross-references found."
      />
    );
  }

  return (
    <div className="space-y-3">
      {refs.map((ref) => (
        <Card key={`${ref.related_paper_id}-${ref.relationship}`}>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">
              Related Paper #{ref.related_paper_id}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm">
            <p>
              <span className="font-medium">Relationship:</span> {ref.relationship}
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

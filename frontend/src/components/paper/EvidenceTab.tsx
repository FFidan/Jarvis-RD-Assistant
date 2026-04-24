import type { Summary } from '@/types';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { EvidenceSnapshot } from '@/components/shared/EvidenceSnapshot';
import { ShieldCheck } from 'lucide-react';

interface EvidenceTabProps {
  summary: Summary | null;
  paperId?: number;
}

export function EvidenceTab({ summary, paperId }: EvidenceTabProps) {
  if (!summary) {
    return (
      <EmptyState
        icon={ShieldCheck}
        title="No summary available"
        description="Generate a summary first to see verified findings."
      />
    );
  }

  const findings = summary.key_findings ?? [];

  if (findings.length === 0) {
    return (
      <EmptyState
        icon={ShieldCheck}
        title="No findings"
        description="No findings to display."
      />
    );
  }

  return (
    <div className="space-y-6">
      <h3 className="text-lg font-semibold">Verified Findings</h3>
      <div className="space-y-3">
        {findings.map((kf, i) => (
          <Card key={i}>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="text-sm font-medium">
                  <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none">{kf.finding}</MarkdownContent>
                </CardTitle>
                <Badge variant={kf.verified ? 'default' : 'secondary'} className="shrink-0 text-xs">
                  {kf.verified ? 'Verified' : 'Unverified'}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="flex gap-3">
                {kf.snapshot_path && paperId != null && kf.page_number != null && (
                  <EvidenceSnapshot
                    paperId={paperId}
                    page={kf.page_number}
                    altText={`Page ${kf.page_number} snapshot`}
                    variant="thumbnail"
                  />
                )}
                <div className="flex-1 space-y-2">
                  {kf.quote && (
                    <blockquote className="border-l-2 border-muted-foreground/30 pl-3 text-sm italic text-muted-foreground">
                      <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none italic">{kf.quote}</MarkdownContent>
                    </blockquote>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {kf.page_number != null && (
                      <Badge variant="outline" className="text-xs">Page {kf.page_number}</Badge>
                    )}
                    {kf.chunk_id != null && (
                      <Badge variant="outline" className="text-xs">Chunk #{kf.chunk_id}</Badge>
                    )}
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

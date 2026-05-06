import type { Summary } from '@/types';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { FileText, AlertTriangle } from 'lucide-react';

interface SummaryTabProps {
  summary: Summary | null;
}

export function SummaryTab({ summary }: SummaryTabProps) {
  if (!summary) {
    return (
      <EmptyState
        icon={FileText}
        title="No summary available"
        description="Use 'Generate Summary' in the sidebar to create one."
      />
    );
  }

  return (
    <div className="space-y-6">
      <section>
        <h3 className="mb-2 text-lg font-semibold">Brief</h3>
        <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">{summary.summary_brief || 'No brief summary.'}</MarkdownContent>
      </section>

      <section>
        <h3 className="mb-2 text-lg font-semibold">Detailed Summary</h3>
        <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">{summary.summary_detailed || 'No detailed summary.'}</MarkdownContent>
      </section>

      <section>
        <h3 className="mb-2 text-lg font-semibold">Methodology</h3>
        <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">{summary.methodology || 'Not available.'}</MarkdownContent>
      </section>

      <section>
        <h3 className="mb-2 text-lg font-semibold">Limitations</h3>
        <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">{summary.limitations || 'Not available.'}</MarkdownContent>
      </section>

      <section>
        <h3 className="mb-2 text-lg font-semibold">Key Findings</h3>
        {summary.key_findings && summary.key_findings.length > 0 ? (
          <div className="space-y-3">
            {summary.key_findings.map((kf) => (
              <Card key={kf.finding} className="rounded-md border-hair shadow-none">
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium">
                    <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none">{kf.finding}</MarkdownContent>
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-1">
                  {kf.quote && (
                    <blockquote className="border-l-2 border-muted-foreground/30 pl-3 text-sm italic text-muted-foreground">
                      &ldquo;{kf.quote}&rdquo;
                    </blockquote>
                  )}
                  <div className="flex flex-wrap gap-2 pt-1">
                    {kf.page_number != null && (
                      <Badge variant="outline" className="text-xs">Page {kf.page_number}</Badge>
                    )}
                    <Badge variant={kf.verified ? 'default' : 'secondary'} className="text-xs">
                      {kf.verified ? 'Verified' : 'Unverified'}
                    </Badge>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No key findings recorded.</p>
        )}
      </section>

      {summary.confidence && (
        <section>
          <h3 className="mb-2 text-lg font-semibold">Confidence Score</h3>
          <Badge
            variant={summary.confidence === 'HIGH' ? 'default' : summary.confidence === 'MEDIUM' ? 'secondary' : 'outline'}
            className="text-sm"
          >
            {summary.confidence}
          </Badge>
        </section>
      )}

      {!summary.summary_verified && (
        <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-[var(--status-warn)] dark:border-amber-900 dark:bg-amber-950">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Summary text is LLM-generated prose. Only key findings with quotes are independently
            verified against the source PDF.
          </span>
        </div>
      )}
    </div>
  );
}

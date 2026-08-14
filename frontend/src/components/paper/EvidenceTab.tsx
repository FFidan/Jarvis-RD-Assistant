import type { Summary } from '@/types';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { EvidenceSnapshot } from '@/components/shared/EvidenceSnapshot';
import { ShieldCheck } from 'lucide-react';

/** PdfReaderPane listens for this; it is lazy-loaded, so the name is repeated
 *  there rather than imported from it (importing would defeat the split). */
const PDF_GOTO_EVENT = 'jarvis:pdf-goto';

/** Jump to the PDF Reader section and ask it to show `page` / flash `quote`. */
function jumpToPdfPage(page: number, quote?: string | null) {
  document.getElementById('section-pdf')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  window.dispatchEvent(
    new CustomEvent(PDF_GOTO_EVENT, { detail: { page, quote: quote ?? null } }),
  );
}

/** Scroll to the Source Passages section, where the cited passage lives. */
function jumpToPassages() {
  document.getElementById('section-chunks')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

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
        {findings.map((kf) => {
          const pageNumber = kf.page_number;
          return (
          <Card key={kf.finding} className="rounded-md border-hair shadow-none">
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
                {kf.snapshot_path && paperId != null && pageNumber != null && (
                  <button
                    type="button"
                    onClick={() => jumpToPdfPage(pageNumber, kf.quote)}
                    className="shrink-0 cursor-pointer rounded transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    aria-label={`Open page ${pageNumber} in the PDF reader`}
                  >
                    <EvidenceSnapshot
                      paperId={paperId}
                      page={pageNumber}
                      altText={`Page ${pageNumber} snapshot`}
                      variant="thumbnail"
                    />
                  </button>
                )}
                <div className="flex-1 space-y-2">
                  {kf.quote && (
                    <blockquote className="border-l-2 border-muted-foreground/30 pl-3 text-sm italic text-muted-foreground">
                      <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none italic">{kf.quote}</MarkdownContent>
                    </blockquote>
                  )}
                  {/* The anchors are the affordances: Page opens the PDF
                      reader at that page and flashes the quote; Passage jumps
                      to Source Passages. */}
                  <div className="flex flex-wrap items-center gap-2">
                    {pageNumber != null && (
                      <button
                        type="button"
                        onClick={() => jumpToPdfPage(pageNumber, kf.quote)}
                        className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        aria-label={`Open page ${pageNumber} in the PDF reader`}
                      >
                        <Badge variant="outline" className="cursor-pointer text-xs hover:bg-accent">
                          Page {pageNumber} →
                        </Badge>
                      </button>
                    )}
                    {kf.chunk_id != null && (
                      <button
                        type="button"
                        onClick={jumpToPassages}
                        className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        aria-label="Open the source passages section"
                      >
                        <Badge variant="outline" className="cursor-pointer text-xs hover:bg-accent">
                          Passage #{kf.chunk_id} →
                        </Badge>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
          );
        })}
      </div>
    </div>
  );
}

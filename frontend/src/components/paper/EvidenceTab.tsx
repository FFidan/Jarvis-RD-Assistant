import type { Chunk, Summary } from '@/types';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { EvidenceSnapshot } from '@/components/shared/EvidenceSnapshot';
import { ShieldCheck } from 'lucide-react';
import { PDF_GOTO_EVENT } from '@/lib/pdf-events';
import { passageAnchorId } from './ChunksTab';

/** Jump to the PDF Reader section and ask it to show `page` / flash `quote`.
 *  PdfReaderPane listens for the event; it is lazy-loaded and importing the
 *  component here would defeat the split, so only the contract is shared. */
function jumpToPdfPage(page: number, quote?: string | null) {
  document.getElementById('section-pdf')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  window.dispatchEvent(
    new CustomEvent(PDF_GOTO_EVENT, { detail: { page, quote: quote ?? null } }),
  );
}

/** Reveal and scroll to the exact source passage for a verified quote. */
function jumpToPassage(chunkId: number) {
  const reveal = () => {
    const passage = document.getElementById(passageAnchorId(chunkId));
    if (!passage) return false;

    const toggle = passage.querySelector<HTMLButtonElement>('button[aria-expanded]');
    if (toggle?.getAttribute('aria-expanded') === 'false') toggle.click();
    passage.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return true;
  };

  if (reveal()) return;

  const section = document.getElementById('section-chunks');
  section?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const sectionToggle = section?.querySelector<HTMLButtonElement>('[data-testid="chunks-expand-toggle"]');
  if (sectionToggle?.getAttribute('aria-expanded') === 'false') sectionToggle.click();
  requestAnimationFrame(reveal);
}

interface EvidenceTabProps {
  summary: Summary | null;
  chunks?: Chunk[];
  paperId?: number;
  /**
   * Whether this paper's PDF is downloaded. The reader section only renders
   * for a downloaded PDF, so page anchors can only act when one is present;
   * without it they say so instead of promising a jump that never happens.
   */
  pdfAvailable?: boolean;
}

export function EvidenceTab({
  summary,
  chunks = [],
  paperId,
  pdfAvailable = false,
}: EvidenceTabProps) {
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
          const passage = chunks.find((chunk) => chunk.id === kf.chunk_id);
          const snapshot =
            kf.snapshot_path && paperId != null && pageNumber != null ? (
              <EvidenceSnapshot
                paperId={paperId}
                page={pageNumber}
                altText={`Page ${pageNumber} snapshot`}
                variant="thumbnail"
              />
            ) : null;
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
                {snapshot &&
                  pageNumber != null &&
                  (pdfAvailable ? (
                    <button
                      type="button"
                      onClick={() => jumpToPdfPage(pageNumber, kf.quote)}
                      className="shrink-0 cursor-pointer rounded transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      aria-label={`Open page ${pageNumber} in the PDF reader`}
                    >
                      {snapshot}
                    </button>
                  ) : (
                    <div className="shrink-0">{snapshot}</div>
                  ))}
                <div className="flex-1 space-y-2">
                  {kf.quote && (
                    <blockquote className="border-l-2 border-muted-foreground/30 pl-3 text-sm italic text-muted-foreground">
                      <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none italic">{kf.quote}</MarkdownContent>
                    </blockquote>
                  )}
                  <div className="flex flex-wrap items-center gap-2">
                    {pageNumber != null &&
                      (pdfAvailable ? (
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
                      ) : (
                        <Badge variant="outline" className="text-xs font-normal text-muted-foreground">
                          Page {pageNumber} — download the PDF to open it here
                        </Badge>
                      ))}
                    {kf.chunk_id != null && (
                      <button
                        type="button"
                        onClick={() => jumpToPassage(kf.chunk_id!)}
                        className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        aria-label={
                          passage
                            ? `Open passage ${passage.chunk_index + 1} of ${chunks.length}`
                            : 'Open the source passage'
                        }
                      >
                        <Badge variant="outline" className="cursor-pointer text-xs hover:bg-accent">
                          {passage
                            ? `Passage ${passage.chunk_index + 1} of ${chunks.length} →`
                            : 'Source passage →'}
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

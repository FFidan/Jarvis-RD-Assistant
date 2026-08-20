/**
 * PaperTOC — the Contents panel of Paper Detail. It docks beside the reading
 * column on wide screens and opens as a sheet on narrow ones.
 *
 * Renders two panels:
 *  § Sections — scroll-jump navigation with active-section highlight.
 *               Badges show per-section counts for Evidence/Cross-refs/
 *               Contradictions/Notes.
 *  § Pipeline — read-only processing status derived from existing paper data.
 */
import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { isProcessingFailed } from '@/lib/paper-pipeline';
import { cn } from '@/lib/utils';

// ---- Types ----------------------------------------------------------------

export interface TOCSection {
  id: string;
  label: string;
  count?: number; // show badge when present
}

export interface PipelineStatus {
  pdfDownloaded: boolean;
  chunkCount: number;
  hasSummary: boolean;
  /** Set when the paper's most recent processing run ended in failure. */
  processingFailed?: boolean;
}

interface PaperTOCProps {
  sections: TOCSection[];
  activeId: string | null;
  pipeline: PipelineStatus;
  /** Called when user clicks a TOC item; scroll is managed by parent. */
  onNavigate: (id: string) => void;
  className?: string;
}

// ---- Pipeline step component ----------------------------------------------

function PipelineStep({
  done,
  inProgress,
  failed,
  label,
}: {
  done: boolean;
  inProgress: boolean;
  failed: boolean;
  label: string;
}) {
  return (
    <div className="flex items-center gap-2 text-sm">
      {failed ? (
        <XCircle className="h-4 w-4 shrink-0 text-destructive" />
      ) : done ? (
        <CheckCircle2 className="h-4 w-4 shrink-0 text-[var(--status-ok)]" />
      ) : inProgress ? (
        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />
      ) : (
        <div className="h-4 w-4 rounded-full border shrink-0" />
      )}
      <span
        className={cn(
          // Done steps are muted, not struck through — strikethrough
          // conventionally reads as cancelled, not completed
          failed
            ? 'font-medium text-destructive'
            : inProgress
              ? 'font-medium'
              : 'text-muted-foreground',
        )}
      >
        {label}
      </span>
    </div>
  );
}

// ---- Main component -------------------------------------------------------

export function PaperTOC({
  sections,
  activeId,
  pipeline,
  onNavigate,
  className,
}: PaperTOCProps) {
  const { pdfDownloaded, chunkCount, hasSummary, processingFailed = false } = pipeline;

  // The processing step's failure rule is shared with the actions panel so the
  // two rails cannot drift apart again; the remaining steps read directly from
  // the paper's persisted counts.
  const processingHasFailed = isProcessingFailed({ processingFailed, hasChunks: chunkCount > 0 });
  const summarizing = pdfDownloaded && chunkCount > 0 && !hasSummary;

  return (
    <nav aria-label="Paper navigation" className={cn('space-y-6 text-sm', className)}>
      {/* § Sections */}
      <div>
        <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Paper sections
        </p>
        <ul className="space-y-0.5">
          {sections.map((sec) => {
            const isActive = activeId === sec.id;
            return (
              <li key={sec.id}>
                <button
                  type="button"
                  data-toc-id={sec.id}
                  onClick={() => onNavigate(sec.id)}
                  className={cn(
                    'flex w-full items-center justify-between rounded px-2 py-1.5 text-left transition-colors',
                    isActive
                      ? 'bg-accent text-accent-foreground font-medium'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                  )}
                  aria-current={isActive ? 'location' : undefined}
                >
                  <span className="truncate">{sec.label}</span>
                  {sec.count !== undefined && sec.count > 0 && (
                    <span
                      className={cn(
                        'ml-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] tabular-nums',
                        isActive
                          ? 'bg-accent-foreground/15 text-accent-foreground'
                          : 'bg-muted text-muted-foreground',
                      )}
                    >
                      {sec.count}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      {/* § Pipeline */}
      <div>
        <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Processing steps
        </p>
        <div className="space-y-1.5 pl-1">
          <PipelineStep
            done={pdfDownloaded}
            inProgress={false}
            failed={false}
            label="Downloaded"
          />
          <PipelineStep
            done={chunkCount > 0}
            inProgress={pdfDownloaded && chunkCount === 0 && !processingFailed}
            failed={processingHasFailed}
            label={chunkCount > 0 ? `${chunkCount} passages` : 'Extracting passages…'}
          />
          <PipelineStep
            done={hasSummary}
            inProgress={summarizing}
            failed={false}
            label={hasSummary ? 'Summarized' : 'Summarizing…'}
          />
        </div>
      </div>
    </nav>
  );
}

/**
 * PaperResearchLog — Center research-log column for the Paper Detail 3-pane.
 *
 * Renders all §-sections in a single scrolling column with proper anchor IDs.
 * Each sub-component is already implemented; this file re-composes them as
 * named, anchored sections (no tabs, no pagination).
 *
 * Chunks are lazy/collapsed — hidden behind a toggle by default.
 */
import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { type Paper, type Summary, type Chunk, type UserState } from '@/types';
import { Badge } from '@/components/ui/badge';
import { SOURCE_LABELS } from '@/components/feed/source-labels';
import { Button } from '@/components/ui/button';
import { EvidenceTab } from './EvidenceTab';
import { ChunksTab } from './ChunksTab';
import { CrossReferencesTab } from './CrossReferencesTab';
import { NotesTab } from './NotesTab';
import { RAGChatSection } from './RAGChatSection';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { formatDate, formatAuthors, cn } from '@/lib/utils';
import { ChevronDown, ChevronRight, ExternalLink, AlertTriangle, ShieldCheck, Wand2 } from 'lucide-react';
import { OfflineIndicator } from '@/components/shared/OfflineIndicator';

// Reuse the canonical discovery-source labels; add the paper-only source types.
const PAPER_SOURCE_LABELS: Record<string, string> = {
  ...SOURCE_LABELS,
  upload: 'Upload',
  doi: 'DOI',
  web: 'Web',
};

// ---- Section wrapper ------------------------------------------------------

function ResearchSection({
  id,
  title,
  children,
  className,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className={cn('scroll-mt-4', className)}>
      <h2
        id={`${id}-heading`}
        className="mb-4 text-base font-semibold text-strong border-b border-hair pb-2"
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

// ---- Props ----------------------------------------------------------------

export interface PaperResearchLogProps {
  paper: Paper;
  summary: Summary | null;
  chunks: Chunk[];
  userState: UserState | null;
  /** Lifecycle state label for breadcrumb display. */
  surfaceLabel?: string;
  /** Optional recommendation score (render only when present). */
  recommendationScore?: number | null;
  paperId: number;
  /** Counts for TOC badges (computed in parent). */
  evidenceCount: number;
  crossRefCount: number;
  contradictionCount: number;
  noteCount: number;
  /**
   * When false (offline): Notes section renders read-only (editor disabled +
   * explanatory hint); Ask This Paper RAG section shows an online-only indicator.
   * Defaults to true so existing callers are unchanged.
   */
  isOnline?: boolean;
}

// ---- Lazy chunks section --------------------------------------------------

function LazyChunksSection({ chunks }: { chunks: Chunk[] }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex items-center gap-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
        aria-expanded={expanded}
        data-testid="chunks-expand-toggle"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        {expanded ? 'Hide' : 'Show'} {chunks.length} passage{chunks.length !== 1 ? 's' : ''}
      </button>
      {expanded && (
        <div className="mt-4">
          <ChunksTab chunks={chunks} />
        </div>
      )}
    </div>
  );
}

// ---- Analyze CTA (shown in Ask section when paper has no chunks) ----------

function AnalyzeCTA() {
  const location = useLocation();
  const analyzeHref = `${location.pathname}?action=analyze`;
  return (
    <div
      data-testid="ask-analyze-cta"
      className="rounded-md border border-hair bg-muted/30 px-4 py-6 text-center text-sm text-muted-foreground"
    >
      <p className="mb-4">
        This paper hasn't been processed yet — Analyze it to enable Ask &amp; Summarize.
      </p>
      <Button asChild variant="default" size="sm">
        <Link to={analyzeHref}>
          <Wand2 className="mr-2 h-4 w-4" />
          Analyze Paper
        </Link>
      </Button>
    </div>
  );
}

// ---- Main component -------------------------------------------------------

export function PaperResearchLog({
  paper,
  summary,
  chunks,
  userState,
  surfaceLabel,
  recommendationScore,
  paperId,
  evidenceCount,
  crossRefCount,
  contradictionCount,
  noteCount,
  isOnline = true,
}: PaperResearchLogProps) {
  const isValidUrl =
    paper.url && (paper.url.startsWith('http://') || paper.url.startsWith('https://'));

  // Resolve lifecycle state for breadcrumb
  const stateLabel =
    surfaceLabel ??
    (userState?.state
      ? userState.state.replace('_', ' ')
      : 'inbox');

  return (
    <div className="space-y-10">
      {/* ── Breadcrumb + score ─────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-4 text-sm text-muted-foreground">
        <nav aria-label="breadcrumb" className="flex items-center gap-1.5">
          <span>Library</span>
          <span aria-hidden>/</span>
          <span className="capitalize">{stateLabel}</span>
          <span aria-hidden>/</span>
          <span className="text-foreground line-clamp-1 max-w-[200px]" title={paper.title}>
            {paper.title}
          </span>
        </nav>
        {recommendationScore != null && (
          <Badge variant="secondary" className="shrink-0 tabular-nums">
            Score {Math.round(recommendationScore * 100)}
          </Badge>
        )}
      </div>

      {/* ── Title + authors ──────────────────────────────────────────── */}
      <div className="space-y-2">
        {/* TL;DR + confidence in header band when available */}
        {summary?.tldr && (
          <div className="flex items-start gap-3 rounded-md border border-hair bg-muted/30 px-4 py-3">
            <span className="shrink-0 text-xs font-semibold uppercase tracking-wide text-muted-foreground mt-0.5">
              TL;DR
            </span>
            <p className="text-sm leading-relaxed">{summary.tldr}</p>
            {summary.confidence && (
              <Badge
                variant={
                  summary.confidence === 'HIGH'
                    ? 'default'
                    : summary.confidence === 'MEDIUM'
                      ? 'secondary'
                      : 'outline'
                }
                className="shrink-0 text-xs"
              >
                {summary.confidence}
              </Badge>
            )}
          </div>
        )}

        {/* summary_verified chip */}
        {summary?.summary_verified && (
          <div className="flex items-center gap-1.5 text-xs text-[var(--status-ok)]">
            <ShieldCheck className="h-3.5 w-3.5" />
            Summary verified against source PDF
          </div>
        )}
        {summary && !summary.summary_verified && (
          <div className="flex items-center gap-1.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-[var(--status-warn)] dark:border-amber-900 dark:bg-amber-950">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            LLM-generated — only key findings with quotes are PDF-verified.
          </div>
        )}

        {summary?.coverage != null && summary.coverage < 1 && (
          <div
            data-testid="coverage-warning"
            className="flex items-center gap-1.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-[var(--status-warn)] dark:border-amber-900 dark:bg-amber-950"
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            {summary.coverage === 0
              ? 'Summary could not be verified — showing abstract-based fallback'
              : `This summary read the first ~${Math.round(summary.coverage * 100)}% of the paper`}
          </div>
        )}
        {summary?.passes != null && summary.passes > 1 && (summary.coverage == null || summary.coverage >= 1) && (
          <div
            data-testid="coverage-note"
            className="flex items-center gap-1.5 text-xs text-muted-foreground"
          >
            Read in {summary.passes} passes — full paper covered
          </div>
        )}

        {/* Serif title */}
        <h1 className="font-serif text-2xl font-bold leading-tight tracking-tight text-strong lg:text-3xl">
          {paper.title}
        </h1>

        {paper.authors.length > 0 && (
          <p className="text-sm text-muted-foreground">{formatAuthors(paper.authors)}</p>
        )}

        <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
          <Badge variant="outline">{PAPER_SOURCE_LABELS[paper.source_type] ?? paper.source_type}</Badge>
          <span>Published: {formatDate(paper.published_date ?? paper.created_at)}</span>
          {paper.citation_count > 0 && (
            <Badge variant="secondary">{paper.citation_count} citations</Badge>
          )}
          {isValidUrl && (
            <a
              href={paper.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
            >
              Open original <ExternalLink className="ml-0.5 h-3 w-3" />
            </a>
          )}
        </div>
      </div>

      {/* ── § Brief ──────────────────────────────────────────────────── */}
      <ResearchSection id="section-brief" title="Brief">
        {summary ? (
          <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">
            {summary.summary_brief || 'No brief summary.'}
          </MarkdownContent>
        ) : (
          <p className="text-sm text-muted-foreground">
            Run Analyze to generate a summary.
          </p>
        )}
      </ResearchSection>

      {/* ── § Detailed ───────────────────────────────────────────────── */}
      <ResearchSection id="section-detailed" title="Detailed Summary">
        {summary ? (
          <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">
            {summary.summary_detailed || 'No detailed summary.'}
          </MarkdownContent>
        ) : (
          <p className="text-sm text-muted-foreground">No detailed summary yet.</p>
        )}
      </ResearchSection>

      {/* ── § Methodology ────────────────────────────────────────────── */}
      <ResearchSection id="section-methodology" title="Methodology">
        {summary?.methodology ? (
          <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">
            {summary.methodology}
          </MarkdownContent>
        ) : (
          <p className="text-sm text-muted-foreground">Not available.</p>
        )}
      </ResearchSection>

      {/* ── § Limitations ────────────────────────────────────────────── */}
      <ResearchSection id="section-limitations" title="Limitations">
        {summary?.limitations ? (
          <MarkdownContent className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">
            {summary.limitations}
          </MarkdownContent>
        ) : (
          <p className="text-sm text-muted-foreground">Not available.</p>
        )}
      </ResearchSection>

      {/* ── § Evidence / Key Findings ────────────────────────────────── */}
      {/* Single section: key_findings with page anchors + Verified chip       */}
      {/* (EvidenceTab). The former duplicate "section-evidence" ghost span    */}
      {/* (zero-height, outside the section) caused scroll-spy jitter and was  */}
      {/* removed — the TOC now has one "Evidence / Key Findings" entry that   */}
      {/* targets section-findings.                                            */}
      <ResearchSection
        id="section-findings"
        title={`Evidence / Key Findings${evidenceCount > 0 ? ` (${evidenceCount})` : ''}`}
      >
        <EvidenceTab summary={summary} paperId={paperId} />
      </ResearchSection>

      {/* ── § Cross-references ───────────────────────────────────────── */}
      <ResearchSection id="section-crossrefs" title={`Cross-references${crossRefCount > 0 ? ` (${crossRefCount})` : ''}`}>
        <CrossReferencesTab summary={summary} />
      </ResearchSection>

      {/* ── § Contradictions ─────────────────────────────────────────── */}
      <ResearchSection id="section-contradictions" title={`Contradictions${contradictionCount > 0 ? ` (${contradictionCount})` : ''}`}>
        {/* ContradictionsPanel is in the right rail (action surface per spec).
            Here we render a read-only summary count with a hint to see the right rail. */}
        {contradictionCount > 0 ? (
          <p className="text-sm text-muted-foreground">
            {contradictionCount} contradiction{contradictionCount !== 1 ? 's' : ''} found — see the
            right panel for details.
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">No contradictions detected yet.</p>
        )}
      </ResearchSection>

      {/* ── § Your notes ─────────────────────────────────────────────── */}
      {/* Offline: notes render read-only (editor disabled). Note *editing* is
          an explicit offline NON-GOAL; existing cached notes remain readable. */}
      <ResearchSection id="section-notes" title={`Your Notes${noteCount > 0 ? ` (${noteCount})` : ''}`}>
        {!isOnline && (
          <div
            data-testid="notes-offline-hint"
            className="mb-3 flex items-center gap-2 rounded border border-hair bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
            role="status"
          >
            <OfflineIndicator variant="online-only" label="Note editing" />
            <span className="ml-1">Notes are read-only offline. Connect to add or edit notes.</span>
          </div>
        )}
        <NotesTab paperId={paperId} readOnly={!isOnline} />
      </ResearchSection>

      {/* ── § Chunks (lazy) ──────────────────────────────────────────── */}
      <ResearchSection id="section-chunks" title={`Source Passages${chunks.length > 0 ? ` (${chunks.length})` : ''}`}>
        {chunks.length > 0 ? (
          <LazyChunksSection chunks={chunks} />
        ) : (
          <p className="text-sm text-muted-foreground">
            Analyze this paper to enable search and Q&amp;A.
          </p>
        )}
      </ResearchSection>

      {/* ── Ask this paper ───────────────────────────────────────────── */}
      {/* Asking questions about a paper is an explicit offline NON-GOAL — show online-only indicator. */}
      <ResearchSection id="section-ask" title="Ask This Paper">
        {!isOnline ? (
          <div
            data-testid="ask-offline-notice"
            className="rounded-md border border-hair bg-muted/30 px-4 py-6 text-center text-sm text-muted-foreground"
          >
            <div className="mb-2 flex justify-center">
              <OfflineIndicator variant="online-only" label="Ask This Paper" />
            </div>
            <p>Asking questions about this paper requires an internet connection and a running model.</p>
          </div>
        ) : chunks.length === 0 ? (
          <AnalyzeCTA />
        ) : (
          <RAGChatSection paperId={paperId} />
        )}
      </ResearchSection>
    </div>
  );
}

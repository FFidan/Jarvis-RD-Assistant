/**
 * PaperDetailPage — reading-first layout for a single paper.
 *
 * A structural toolbar sits above the page's own scroll region, so Back, the
 * running title, lifecycle, citation export and the two panel toggles stay
 * reachable at any reading depth. The research log scrolls in the middle at a
 * fixed reading measure; Contents and Actions open as DOCKED, non-modal side
 * panels on wide screens — opening one narrows the column instead of freezing
 * it behind an overlay, so scrolling continues and the Contents list keeps
 * highlighting the current section. Below that width they open as sheets.
 *
 * The Contents pipeline list and the actions panel's step tracker both read
 * the paper-detail payload's `processing_failed` flag and apply the same
 * shared `isProcessingFailed` rule to it, so a reload cannot show one failed
 * and the other pending.
 */
import { useParams, Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { FileText, ArrowLeft, Menu, List } from 'lucide-react';
import { useState, useEffect, useCallback, useMemo } from 'react';
import { fetchPaperDetail, fetchNotes, fetchContradictions } from '@/lib/api';
import { useOnlineStatus } from '@/hooks/use-online-status';
import { getPersistedCacheTimestamp } from '@/lib/query-persister';
import { OfflineIndicator } from '@/components/shared/OfflineIndicator';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { ActionsSidebar } from '@/components/paper/ActionsSidebar';
import { LifecycleActionsCard } from '@/components/paper/LifecycleActionsCard';
import { ZoteroPanel } from '@/components/paper/ZoteroPanel';
import { CitationMenu } from '@/components/citation/CitationMenu';
import { ContradictionsPanel } from '@/components/paper/ContradictionsPanel';
import { PaperTOC, type TOCSection } from '@/components/paper/PaperTOC';
import { PaperResearchLog } from '@/components/paper/PaperResearchLog';
import { usePaperScrollSpy } from '@/hooks/paper-scroll-spy';
import { errorMessage } from '@/lib/errors';
import { cn } from '@/lib/utils';

// Section IDs in document order (must match ResearchSection ids in PaperResearchLog)
const SECTION_IDS = [
  'section-brief',
  'section-detailed',
  'section-methodology',
  'section-limitations',
  'section-findings',
  'section-pdf',
  'section-crossrefs',
  'section-contradictions',
  'section-notes',
  'section-chunks',
  'section-ask',
];

/** Long enough for the sheet's close animation to finish and release focus. */
const SHEET_CLOSE_SETTLE_MS = 200;

export function PaperDetailPage() {
  const { paperId: paramId } = useParams<{ paperId: string }>();
  const paperId = Number(paramId);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [actionSheetOpen, setActionSheetOpen] = useState(false);
  const [tocSheetOpen, setTocSheetOpen] = useState(false);
  const [tocDocked, setTocDocked] = useState(false);
  const [actionsDocked, setActionsDocked] = useState(false);
  const [processPulse, setProcessPulse] = useState(false);
  const [analyzePulse, setAnalyzePulse] = useState(false);
  const { online } = useOnlineStatus();
  const [cacheTimestamp, setCacheTimestamp] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    getPersistedCacheTimestamp().then((ts) => {
      if (!cancelled) setCacheTimestamp(ts);
    });
    return () => { cancelled = true; };
  }, []);

  // Main data query
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.papers.detail(paperId),
    queryFn: () => fetchPaperDetail(paperId),
    enabled: !isNaN(paperId) && paperId > 0,
  });

  // Count queries for Contents badges
  const { data: userNotesData = [] } = useQuery({
    queryKey: QUERY_KEYS.notes.user(paperId),
    queryFn: () => fetchNotes(paperId, 'user'),
    enabled: !isNaN(paperId) && paperId > 0,
  });
  const { data: contradictionsData } = useQuery({
    queryKey: QUERY_KEYS.contradictions.verified(paperId),
    queryFn: () => fetchContradictions({ paper_id: paperId, status: 'verified', limit: 20 }),
    enabled: !isNaN(paperId) && paperId > 0,
  });

  // Scroll-spy — active section for the Contents highlight. The section
  // anchors only exist once the paper has loaded, so the ids are withheld
  // until then: handing the hook a new array is what re-runs its observer
  // setup against a DOM that now contains the sections.
  const spySectionIds = useMemo(() => (data ? SECTION_IDS : []), [data]);
  const activeId = usePaperScrollSpy(spySectionIds);

  // Scroll a section into view when the reader picks it from Contents.
  // The docked panel is not in the way, so it scrolls straight away. The
  // sheet must CLOSE FIRST and scroll only once the close has settled: Radix
  // restores focus to the trigger on close, and that focus scrolls the
  // trigger into view, cancelling any in-flight smooth scroll.
  const handleTocNavigate = useCallback(
    (id: string) => {
      const scrollToSection = () =>
        document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (!tocSheetOpen) {
        scrollToSection();
        return;
      }
      setTocSheetOpen(false);
      window.setTimeout(scrollToSection, SHEET_CLOSE_SETTLE_MS);
    },
    [tocSheetOpen],
  );

  // ?action=process — scroll the Process PDF button into view
  useEffect(() => {
    if (!data) return;
    if (searchParams.get('action') !== 'process') return;
    const el = document.getElementById('paper-action-process');
    if (!el) return;
    const details = el.closest('details');
    if (details && !details.open) details.open = true;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setProcessPulse(true);
    const t = setTimeout(() => setProcessPulse(false), 1500);
    return () => clearTimeout(t);
  }, [data, searchParams]);

  // ?action=analyze — scroll the Analyze Paper button into view + pulse it
  useEffect(() => {
    if (!data) return;
    if (searchParams.get('action') !== 'analyze') return;
    const el = document.getElementById('paper-action-analyze');
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setAnalyzePulse(true);
    const t = setTimeout(() => setAnalyzePulse(false), 1500);
    return () => clearTimeout(t);
  }, [data, searchParams]);

  // ── Error states ──────────────────────────────────────────────────────────

  if (isNaN(paperId) || paperId <= 0) {
    return (
      <div className="space-y-4">
        <h1 className="flex items-center gap-2 text-[32px] leading-tight tracking-tight text-strong">
          <FileText className="h-8 w-8" /> Paper Detail
        </h1>
        <p className="text-muted-foreground">
          Invalid paper ID. <Link to="/feed" className="underline">Go back to feed</Link>.
        </p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-3/4" />
        <Skeleton className="h-5 w-1/2" />
        <div className="flex gap-2">
          <Skeleton className="h-6 w-20" />
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-6 w-24" />
        </div>
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="space-y-4">
        <h1 className="flex items-center gap-2 text-[32px] leading-tight tracking-tight text-strong">
          <FileText className="h-8 w-8" /> Paper Detail
        </h1>
        <p className="text-destructive">
          {errorMessage(error, 'Failed to load paper.')}
        </p>
        <Link to="/feed" className="text-sm underline">Go back to feed</Link>
      </div>
    );
  }

  if (!data) return null;

  const { paper, summary, chunks, user_state } = data;
  const hasProjectLinks = Boolean(data.has_project_links);
  // Same persisted failure signal ActionsSidebar polls via getJob — surfaced
  // on the paper-detail payload so the Contents pipeline list agrees with it.
  const processingFailed = Boolean(data.processing_failed);

  // ── Derived counts for Contents badges ────────────────────────────────────
  const evidenceCount = summary?.key_findings?.length ?? 0;
  const crossRefCount = summary?.cross_references?.length ?? 0;
  const contradictionCount = contradictionsData?.contradictions?.length ?? 0;
  const noteCount = Array.isArray(userNotesData) ? userNotesData.length : 0;

  const tocSections: TOCSection[] = [
    { id: 'section-brief', label: 'Brief' },
    { id: 'section-detailed', label: 'Detailed Summary' },
    { id: 'section-methodology', label: 'Methodology' },
    { id: 'section-limitations', label: 'Limitations' },
    { id: 'section-findings', label: 'Evidence / Key Findings', count: evidenceCount },
    { id: 'section-pdf', label: 'PDF Reader' },
    { id: 'section-crossrefs', label: 'Related work', count: crossRefCount },
    { id: 'section-contradictions', label: 'Contradictions', count: contradictionCount },
    { id: 'section-notes', label: 'Your Notes', count: noteCount },
    { id: 'section-chunks', label: 'Source Passages', count: chunks.length },
    { id: 'section-ask', label: 'Ask This Paper' },
  ];

  const paperTOC = (
    <PaperTOC
      sections={tocSections}
      activeId={activeId}
      pipeline={{
        pdfDownloaded: paper.pdf_downloaded,
        chunkCount: chunks.length,
        hasSummary: summary !== null,
        processingFailed,
      }}
      onNavigate={handleTocNavigate}
    />
  );

  // ── Actions panel ─────────────────────────────────────────────────────────
  // Pipeline actions (Analyze/Download/Process/Summarize/Generate Cards),
  // Zotero push and contradiction recompute are all online-only. One overlay
  // on the whole panel communicates that, rather than threading `online` into
  // every child; the children stay mounted so cached pipeline STATUS shows.
  // Reading state, star and citation export live in the toolbar instead.
  const actionsPanelContent = (
    <div className="space-y-2">
      {!online && (
        <div
          data-testid="action-rail-offline-banner"
          className="flex items-center gap-2 rounded-md border border-hair bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
          role="status"
        >
          <OfflineIndicator variant="online-only" label="Pipeline actions" />
          <span className="ml-1">Actions unavailable offline — read-only mode.</span>
        </div>
      )}
      <div
        className={cn('space-y-6', !online && 'pointer-events-none select-none opacity-50')}
        aria-disabled={!online ? true : undefined}
        data-testid={!online ? 'action-rail-disabled' : undefined}
      >
        <ActionsSidebar
          paperId={paperId}
          pdfDownloaded={paper.pdf_downloaded}
          hasChunks={chunks.length > 0}
          hasSummary={summary !== null}
          processingFailed={processingFailed}
          pulseProcessButton={processPulse}
          pulseAnalyzeButton={analyzePulse}
          discoveryOrigin={paper.discovery_origin ?? 'user_initiated'}
          recentFeedback={paper.recent_feedback ?? null}
          state={user_state?.state ?? 'inbox'}
        />
        <ZoteroPanel paperId={paperId} hasProjectLinks={hasProjectLinks} />
        <ContradictionsPanel paperId={paperId} />
        <p className="text-xs text-muted-foreground">
          Reading state, star, and citation export live in the toolbar above the paper. Notes
          belong in the Your Notes section.
        </p>
      </div>
    </div>
  );

  // ── Render ────────────────────────────────────────────────────────────────
  // -m-6 cancels the AppShell padding so the toolbar and the scroller own the
  // full pane, the same structure ResearchFeedPage uses.
  return (
    <div className="-m-6 flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-hair bg-paper px-6 py-2">
        <div className="flex min-w-0 items-center gap-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => (window.history.length > 1 ? navigate(-1) : navigate('/feed'))}
          >
            <ArrowLeft className="mr-1 h-4 w-4" /> Back
          </Button>
          {/* Running title — orients the reader once the heading has scrolled away */}
          <span className="hidden min-w-0 truncate text-sm text-muted-foreground md:inline">
            {paper.title}
          </span>
          {/* Stale-cached when the cache timestamp is known, otherwise
              "available offline" — the reading column stays usable either way. */}
          {!online && (
            <span data-testid="paper-detail-offline-indicator">
              {cacheTimestamp != null ? (
                <OfflineIndicator variant="stale-cached" timestamp={cacheTimestamp} />
              ) : (
                <OfflineIndicator variant="available-offline" />
              )}
            </span>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <LifecycleActionsCard
            paperId={paperId}
            paperTitle={paper.title}
            state={user_state?.state ?? 'inbox'}
            starred={user_state?.starred ?? false}
            variant="toolbar"
          />
          <CitationMenu paperIds={[paperId]} />
          <div className="mx-1 h-5 w-px bg-hair" aria-hidden="true" />

          {/* Wide screens: the panels dock beside the reading column */}
          <Button
            variant={tocDocked ? 'secondary' : 'outline'}
            size="sm"
            className="hidden lg:inline-flex"
            onClick={() => setTocDocked((v) => !v)}
            aria-pressed={tocDocked}
            data-testid="toc-dock-toggle"
          >
            <List className="mr-1 h-4 w-4" /> Contents
          </Button>
          <Button
            variant={actionsDocked ? 'secondary' : 'outline'}
            size="sm"
            className="hidden lg:inline-flex"
            onClick={() => setActionsDocked((v) => !v)}
            aria-pressed={actionsDocked}
            data-testid="actions-dock-toggle"
          >
            <Menu className="mr-1 h-4 w-4" /> Actions
          </Button>

          {/* Narrow screens: there is no room to dock, so they open as sheets */}
          <div className="flex items-center gap-2 lg:hidden">
            <Sheet open={tocSheetOpen} onOpenChange={setTocSheetOpen}>
              <SheetTrigger asChild>
                <Button variant="outline" size="sm" data-testid="toc-sheet-trigger">
                  <List className="mr-1 h-4 w-4" /> Contents
                </Button>
              </SheetTrigger>
              <SheetContent
                side="left"
                className="w-72 overflow-y-auto"
                onCloseAutoFocus={(e) => e.preventDefault()}
              >
                <SheetHeader>
                  <SheetTitle>Sections</SheetTitle>
                </SheetHeader>
                <div className="mt-4">{paperTOC}</div>
              </SheetContent>
            </Sheet>

            <Sheet open={actionSheetOpen} onOpenChange={setActionSheetOpen}>
              <SheetTrigger asChild>
                <Button variant="outline" size="sm" data-testid="actions-sheet-trigger">
                  <Menu className="mr-1 h-4 w-4" /> Actions
                </Button>
              </SheetTrigger>
              <SheetContent side="right" className="w-96 overflow-y-auto sm:max-w-96">
                <SheetHeader>
                  <SheetTitle>Paper Actions</SheetTitle>
                </SheetHeader>
                {/* pr-2 keeps wide buttons (e.g. project linking) off the sheet edge */}
                <div className="mt-4 pr-2">{actionsPanelContent}</div>
              </SheetContent>
            </Sheet>
          </div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        {tocDocked && (
          <aside
            className="hidden w-64 shrink-0 overflow-y-auto border-r border-hair px-4 py-6 lg:block"
            data-testid="toc-dock"
          >
            {paperTOC}
          </aside>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
          <main className="mx-auto w-full min-w-0 max-w-[860px]">
            <PaperResearchLog
              paper={paper}
              summary={summary}
              chunks={chunks}
              userState={user_state}
              surfaceLabel={user_state?.state?.replace('_', ' ')}
              recommendationScore={null /* Paper-detail has no recommendation_score */}
              paperId={paperId}
              evidenceCount={evidenceCount}
              crossRefCount={crossRefCount}
              contradictionCount={contradictionCount}
              noteCount={noteCount}
              isOnline={online}
            />
          </main>
        </div>

        {actionsDocked && (
          <aside
            className="hidden w-96 shrink-0 overflow-y-auto border-l border-hair px-4 py-6 lg:block"
            data-testid="actions-dock"
          >
            {actionsPanelContent}
          </aside>
        )}
      </div>
    </div>
  );
}

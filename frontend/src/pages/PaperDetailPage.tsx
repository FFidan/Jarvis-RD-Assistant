/**
 * PaperDetailPage — 3-pane research-log layout.
 *
 * LEFT  rail: § Sections TOC (scroll-spy active) + § Pipeline read-only status.
 * CENTER:     single scrolling research-log column (all sections anchored).
 * RIGHT rail: action surface (ActionsSidebar + UserStateForm + ZoteroPanel +
 *             ContradictionsPanel) — preserved functionally from previous layout.
 *
 * Both rails collapse to Sheet on small screens.
 * The § Pipeline rail and the actions panel's step tracker both derive their
 * failure state from the paper-detail payload's `processing_failed` (latest
 * paper.process/analyze job status='failed') via the shared
 * `derivePipelineStatus` selector, so a reload cannot show one rail failed
 * and the other pending.
 */
import { useParams, Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { FileText, ArrowLeft, Menu, List } from 'lucide-react';
import { useState, useEffect, useCallback } from 'react';
import { useUIStore } from '@/stores/ui-store';
import { fetchPaperDetail, fetchNotes, fetchContradictions } from '@/lib/api';
import { useOnlineStatus } from '@/hooks/use-online-status';
import { getPersistedCacheTimestamp } from '@/lib/query-persister';
import { OfflineIndicator } from '@/components/shared/OfflineIndicator';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { ActionsSidebar } from '@/components/paper/ActionsSidebar';
import { LifecycleActionsCard } from '@/components/paper/LifecycleActionsCard';
import { UserStateForm } from '@/components/paper/UserStateForm';
import { ZoteroPanel } from '@/components/paper/ZoteroPanel';
import { CitationMenu } from '@/components/citation/CitationMenu';
import { ContradictionsPanel } from '@/components/paper/ContradictionsPanel';
import { PaperTOC, type TOCSection } from '@/components/paper/PaperTOC';
import { PaperResearchLog } from '@/components/paper/PaperResearchLog';
import { ScrollArea } from '@/components/ui/scroll-area';
import { usePaperScrollSpy } from '@/hooks/paper-scroll-spy';
import { errorMessage } from '@/lib/errors';

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

export function PaperDetailPage() {
  const { paperId: paramId } = useParams<{ paperId: string }>();
  const paperId = Number(paramId);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [actionSheetOpen, setActionSheetOpen] = useState(false);
  const [tocSheetOpen, setTocSheetOpen] = useState(false);
  const [processPulse, setProcessPulse] = useState(false);
  const [analyzePulse, setAnalyzePulse] = useState(false);
  const paperDetailNoteDismissed = useUIStore((s) => s.paperDetailNoteDismissed);
  const setPaperDetailNoteDismissed = useUIStore((s) => s.setPaperDetailNoteDismissed);
  const { online } = useOnlineStatus();
  const [cacheTimestamp, setCacheTimestamp] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    getPersistedCacheTimestamp().then((ts) => {
      if (!cancelled) setCacheTimestamp(ts);
    });
    return () => { cancelled = true; };
  }, []);

  // Main data query (unchanged from previous layout)
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.papers.detail(paperId),
    queryFn: () => fetchPaperDetail(paperId),
    enabled: !isNaN(paperId) && paperId > 0,
  });

  // Count queries for TOC badges
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

  // Scroll-spy — active section for left rail highlight
  const activeId = usePaperScrollSpy(SECTION_IDS);

  // Scroll a section into view when user clicks a TOC item
  const handleTocNavigate = useCallback((id: string) => {
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTocSheetOpen(false);
  }, []);

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
  // on the paper-detail payload so the left Pipeline rail shows ✗.
  const processingFailed = Boolean(data.processing_failed);

  // ── Derived counts for TOC badges ─────────────────────────────────────────
  const evidenceCount = summary?.key_findings?.length ?? 0;
  const crossRefCount = summary?.cross_references?.length ?? 0;
  const contradictionCount = contradictionsData?.contradictions?.length ?? 0;
  const noteCount = Array.isArray(userNotesData) ? userNotesData.length : 0;

  // ── TOC sections ──────────────────────────────────────────────────────────
  const tocSections: TOCSection[] = [
    { id: 'section-brief', label: 'Brief' },
    { id: 'section-detailed', label: 'Detailed Summary' },
    { id: 'section-methodology', label: 'Methodology' },
    { id: 'section-limitations', label: 'Limitations' },
    { id: 'section-findings', label: 'Evidence / Key Findings', count: evidenceCount },
    { id: 'section-pdf', label: 'PDF Reader' },
    { id: 'section-crossrefs', label: 'Cross-references', count: crossRefCount },
    { id: 'section-contradictions', label: 'Contradictions', count: contradictionCount },
    { id: 'section-notes', label: 'Your Notes', count: noteCount },
    { id: 'section-chunks', label: 'Source Passages', count: chunks.length },
    { id: 'section-ask', label: 'Ask This Paper' },
  ];

  // ── Action rail (right) ───────────────────────────────────────────────────
  // When offline, pipeline actions (ActionsSidebar — Analyze/Download/Process/
  // Summarize/Generate Cards), Zotero push, and contradiction recompute are
  // all online-only. We overlay the action rail with a disabled state + indicator
  // rather than passing `online` into every sub-component.
  // LifecycleActionsCard (star/state changes) and UserStateForm (tags/notes
  // metadata) are also online-only mutations; they follow the same overlay.
  const actionRailContent = (
    <div className="space-y-2">
      {/* Offline: online-only overlay on the whole action rail */}
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
      {/* Render components; pointer-events + opacity communicate disabled state.
          Components are still mounted so cached pipeline STATUS still shows. */}
      <div
        className={!online ? 'pointer-events-none select-none opacity-50' : undefined}
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
        <LifecycleActionsCard
          paperId={paperId}
          paperTitle={paper.title}
          state={user_state?.state ?? 'inbox'}
          starred={user_state?.starred ?? false}
        />
        <UserStateForm paperId={paperId} userState={user_state} />
        <ZoteroPanel paperId={paperId} hasProjectLinks={hasProjectLinks} />
        <div className="mt-3">
          <CitationMenu paperIds={[paperId]} />
        </div>
        <ContradictionsPanel paperId={paperId} />
      </div>
    </div>
  );

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-4">
      {/* Top bar: back + offline indicator + mobile rail triggers */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => (window.history.length > 1 ? navigate(-1) : navigate('/feed'))}
          >
            <ArrowLeft className="mr-1 h-4 w-4" /> Back
          </Button>
          {/* Offline indicator in paper header — stale-cached when timestamp known,
              otherwise "available offline" (reading column + cached sections stay usable). */}
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

        <div className="flex items-center gap-2 lg:hidden">
          {/* TOC sheet trigger (small screens) */}
          <Sheet open={tocSheetOpen} onOpenChange={setTocSheetOpen}>
            <SheetTrigger asChild>
              <Button variant="outline" size="sm">
                <List className="mr-1 h-4 w-4" /> Contents
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-72 overflow-y-auto">
              <SheetHeader>
                <SheetTitle>Sections</SheetTitle>
              </SheetHeader>
              <div className="mt-4">
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
              </div>
            </SheetContent>
          </Sheet>

          {/* Action sheet trigger (small screens) */}
          <Sheet open={actionSheetOpen} onOpenChange={setActionSheetOpen}>
            <SheetTrigger asChild>
              <Button variant="outline" size="sm">
                <Menu className="mr-1 h-4 w-4" /> Actions
              </Button>
            </SheetTrigger>
            <SheetContent side="right" className="w-80 overflow-y-auto">
              <SheetHeader>
                <SheetTitle>Paper Actions</SheetTitle>
              </SheetHeader>
              <div className="mt-4">{actionRailContent}</div>
            </SheetContent>
          </Sheet>
        </div>
      </div>

      {/* Dismissible workspace note */}
      {!paperDetailNoteDismissed && (
        <div className="border border-border/60 bg-muted/30 rounded p-4 flex justify-between gap-4 text-sm">
          <p>
            Paper Detail is the workspace for a single paper. Run Analyze to
            download+process+summarize, then explore via the §-sections or ask questions
            via &quot;Ask This Paper&quot;.
          </p>
          <button
            onClick={() => setPaperDetailNoteDismissed(true)}
            aria-label="Dismiss"
            className="text-muted-foreground hover:text-foreground"
          >
            ×
          </button>
        </div>
      )}

      {/* 3-pane grid */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[220px_1fr_300px]">
        {/* LEFT rail — desktop only */}
        <aside className="hidden lg:block">
          <div className="sticky top-4">
            <ScrollArea className="h-[calc(100dvh-8rem)]">
              <Card className="rounded-md border-hair shadow-none p-4">
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
              </Card>
            </ScrollArea>
          </div>
        </aside>

        {/* CENTER — research-log scrolling column */}
        <main className="min-w-0">
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

        {/* RIGHT rail — desktop only */}
        <aside className="hidden lg:block">
          <div className="sticky top-4">
            <ScrollArea className="h-[calc(100dvh-8rem)]">
              <Card className="rounded-md border-hair shadow-none p-4">
                {actionRailContent}
              </Card>
            </ScrollArea>
          </div>
        </aside>
      </div>
    </div>
  );
}

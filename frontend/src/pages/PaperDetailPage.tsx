import { useParams, Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { FileText, ArrowLeft, Menu } from 'lucide-react';
import { useState, useEffect } from 'react';
import { useUIStore } from '@/stores/ui-store';
import { fetchPaperDetail } from '@/lib/api';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { PaperHeader } from '@/components/paper/PaperHeader';
import { ActionsSidebar } from '@/components/paper/ActionsSidebar';
import { UserStateForm } from '@/components/paper/UserStateForm';
import { SummaryTab } from '@/components/paper/SummaryTab';
import { EvidenceTab } from '@/components/paper/EvidenceTab';
import { ChunksTab } from '@/components/paper/ChunksTab';
import { CrossReferencesTab } from '@/components/paper/CrossReferencesTab';
import { NotesTab } from '@/components/paper/NotesTab';
import { RAGChatSection } from '@/components/paper/RAGChatSection';
import { ZoteroPanel } from '@/components/paper/ZoteroPanel';
import { ContradictionsPanel } from '@/components/paper/ContradictionsPanel';
import { ScrollArea } from '@/components/ui/scroll-area';

export function PaperDetailPage() {
  const { paperId: paramId } = useParams<{ paperId: string }>();
  const paperId = Number(paramId);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [sheetOpen, setSheetOpen] = useState(false);
  const [processPulse, setProcessPulse] = useState(false);
  const paperDetailNoteDismissed = useUIStore((s) => s.paperDetailNoteDismissed);
  const setPaperDetailNoteDismissed = useUIStore((s) => s.setPaperDetailNoteDismissed);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['paper-detail', paperId],
    queryFn: () => fetchPaperDetail(paperId),
    enabled: !isNaN(paperId) && paperId > 0,
  });

  // If ?action=process, expand Manual steps and scroll the Process PDF button
  // into view with a brief animate-pulse. Waits for data to be available so
  // the scroll target exists in the DOM before attempting the scroll.
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

  if (isNaN(paperId) || paperId <= 0) {
    return (
      <div className="space-y-4">
        <h1 className="flex items-center gap-2 text-3xl font-bold">
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
        <h1 className="flex items-center gap-2 text-3xl font-bold">
          <FileText className="h-8 w-8" /> Paper Detail
        </h1>
        <p className="text-destructive">
          {error instanceof Error ? error.message : 'Failed to load paper.'}
        </p>
        <Link to="/feed" className="text-sm underline">Go back to feed</Link>
      </div>
    );
  }

  if (!data) return null;

  const { paper, summary, chunks, user_state } = data;
  const hasProjectLinks = Boolean(data.has_project_links);

  const sidebarContent = (
    <div className="space-y-2">
      <ActionsSidebar
        paperId={paperId}
        pdfDownloaded={paper.pdf_downloaded}
        hasChunks={chunks.length > 0}
        hasSummary={summary !== null}
        pulseProcessButton={processPulse}
      />
      <UserStateForm paperId={paperId} userState={user_state} />
      <ZoteroPanel paperId={paperId} hasProjectLinks={hasProjectLinks} />
      <ContradictionsPanel paperId={paperId} />
    </div>
  );

  return (
    <div className="space-y-6">
      {/* Back button + mobile sidebar trigger */}
      <div className="flex items-center justify-between">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => window.history.length > 1 ? navigate(-1) : navigate('/feed')}
        >
          <ArrowLeft className="mr-1 h-4 w-4" /> Back
        </Button>

        {/* Mobile sidebar trigger */}
        <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
          <SheetTrigger asChild>
            <Button variant="outline" size="sm" className="lg:hidden">
              <Menu className="mr-1 h-4 w-4" /> Actions
            </Button>
          </SheetTrigger>
          <SheetContent side="right" className="w-80 overflow-y-auto">
            <SheetHeader>
              <SheetTitle>Paper Actions</SheetTitle>
            </SheetHeader>
            <div className="mt-4">{sidebarContent}</div>
          </SheetContent>
        </Sheet>
      </div>

      {/* Main layout: content + desktop sidebar */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_300px]">
        {/* Main content */}
        <div className="min-w-0 space-y-6">
          {!paperDetailNoteDismissed && (
            <div className="border border-border/60 bg-muted/30 rounded p-4 flex justify-between gap-4 text-sm">
              <p>Paper Detail is the workspace for a single paper. Run Analyze to download+process+summarize, then explore via Summary / Evidence / Chunks / Cross-References tabs, ask questions via the Ask tab (Research Feed), or turn it into flashcards.</p>
              <button onClick={() => setPaperDetailNoteDismissed(true)} aria-label="Dismiss" className="text-muted-foreground hover:text-foreground">×</button>
            </div>
          )}
          <PaperHeader paper={paper} isStarred={user_state?.starred === true} />

          <Tabs defaultValue="summary">
            <TabsList className="flex-wrap">
              <TabsTrigger value="summary">Summary</TabsTrigger>
              <TabsTrigger value="evidence">Evidence</TabsTrigger>
              <TabsTrigger value="chunks">Chunks</TabsTrigger>
              <TabsTrigger value="crossrefs">Cross-References</TabsTrigger>
              <TabsTrigger value="notes">Notes</TabsTrigger>
            </TabsList>

            <TabsContent value="summary" className="mt-4">
              <SummaryTab summary={summary} />
            </TabsContent>

            <TabsContent value="evidence" className="mt-4">
              <EvidenceTab summary={summary} paperId={paperId} />
            </TabsContent>

            <TabsContent value="chunks" className="mt-4">
              <ChunksTab chunks={chunks} />
            </TabsContent>

            <TabsContent value="crossrefs" className="mt-4">
              <CrossReferencesTab summary={summary} />
            </TabsContent>

            <TabsContent value="notes" className="mt-4">
              <NotesTab paperId={paperId} />
            </TabsContent>
          </Tabs>

          <RAGChatSection paperId={paperId} />
        </div>

        {/* Desktop sidebar */}
        <aside className="hidden lg:block">
          <div className="sticky top-4">
            <ScrollArea className="h-[calc(100vh-8rem)]">
              <div className="rounded-lg border bg-card p-4">
                {sidebarContent}
              </div>
            </ScrollArea>
          </div>
        </aside>
      </div>
    </div>
  );
}

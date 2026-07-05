import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Link } from 'react-router-dom';
import {
  CheckCircle2, Circle, ArrowRight, X,
  Loader2, ChevronDown, ChevronRight, Cog, FileText, Sparkles,
} from 'lucide-react';
import { fetchDashboardMetrics, batchProcessPapers, batchSummarizePapers, batchExtractEntities } from '@/lib/api';
import { MetricTileGrid } from '@/components/home/MetricTileGrid';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useUIStore } from '@/stores/ui-store';
import { errorMessage } from '@/lib/errors';
import { SetupBanner } from '@/components/setup/SetupBanner';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useConfirm } from '@/hooks/use-confirm';
import { useJobStore } from '@/stores/job-store';
import { useAuthStore } from '@/stores/auth-store';

interface BatchButtonProps<T> {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  mutationFn: () => Promise<T>;
  formatResult: (data: T) => string;
  confirmMessage?: string;
  confirmTitle?: string;
  onSuccessResult?: (data: T) => void;
}

function BatchButton<T>({
  label,
  icon: Icon,
  mutationFn,
  formatResult,
  confirmMessage,
  confirmTitle,
  onSuccessResult,
}: BatchButtonProps<T>) {
  const queryClient = useQueryClient();
  const { isOpen, confirm, handleConfirm, handleCancel } = useConfirm();
  const mutation = useMutation({
    mutationFn,
    onSuccess: (data) => {
      onSuccessResult?.(data);
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.dashboard.metrics() });
    },
  });

  const handleClick = async () => {
    if (confirmMessage) {
      const confirmed = await confirm();
      if (!confirmed) return;
    }
    mutation.mutate();
  };

  return (
    <div className="flex flex-col items-start gap-1">
      <Button
        variant="outline"
        size="sm"
        onClick={handleClick}
        disabled={mutation.isPending}
      >
        {mutation.isPending ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <Icon className="mr-2 h-4 w-4" />
        )}
        {label}
      </Button>
      {mutation.isSuccess && (
        <span className="text-xs text-[var(--status-ok)]">
          {formatResult(mutation.data)}
        </span>
      )}
      {mutation.isError && (
        <span className="text-xs text-[var(--status-bad)]">
          Failed: {errorMessage(mutation.error)}
        </span>
      )}
      {confirmMessage && (
        <ConfirmDialog
          open={isOpen}
          title={confirmTitle ?? 'Are you sure?'}
          description={confirmMessage}
          confirmLabel="Continue"
          onConfirm={handleConfirm}
          onCancel={handleCancel}
        />
      )}
    </div>
  );
}

const BATCH_LIMIT = 10;

type BatchJobResponse = { job_id: string | null };

export function HomePage() {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);
  const isAdmin = useAuthStore((s) => s.user?.role === 'admin');
  const { data: metrics, isLoading, isError, isSuccess } = useQuery({
    queryKey: QUERY_KEYS.dashboard.metrics(),
    queryFn: fetchDashboardMetrics,
    refetchInterval: 60_000,
  });

  const { checklistDismissed, dismissChecklist, onboardingCelebrated, markOnboardingCelebrated } =
    useUIStore();

  const stage = metrics?.onboarding_stage ?? 'needs_topics';
  const hasTopics = stage !== 'needs_topics';
  const hasPapers = hasTopics && stage !== 'needs_papers';
  const hasProcessedPapers = hasPapers && stage === 'complete';

  const showChecklist = !checklistDismissed && metrics?.onboarding_stage !== 'complete' && isSuccess;
  const trackBatchJob = (data: BatchJobResponse, kind: string) => {
    if (data.job_id == null) return;
    trackExternalJob({
      jobId: data.job_id,
      kind,
      payload: { limit: BATCH_LIMIT },
      status: 'queued',
    });
  };

  // One-time onboarding-complete celebration. Visibility is latched into local
  // state BEFORE the persisted flag flips: marking celebrated immediately makes
  // celebrationEligible false, so without the latch the card would vanish on the
  // very next render. The eligibility guard also makes the effect fire exactly
  // once — after the flag is set, re-runs (and remounts/reloads) are no-ops.
  const [showCelebration, setShowCelebration] = useState(false);
  const celebrationEligible =
    !isLoading && metrics?.onboarding_stage === 'complete' && !onboardingCelebrated;
  useEffect(() => {
    if (celebrationEligible) {
      setShowCelebration(true);
      markOnboardingCelebrated();
    }
  }, [celebrationEligible, markOnboardingCelebrated]);

  const steps = [
    {
      done: hasTopics,
      label: 'Add a research topic',
      description: 'Tell JARVIS what you research',
      actionLabel: 'Go to Settings',
      actionHref: '/settings',
    },
    {
      done: hasPapers,
      label: 'Fetch your first papers',
      description: 'Search arXiv and Semantic Scholar',
      actionLabel: 'Open Library',
      actionHref: '/feed?surface=library',
      disabled: !hasTopics,
    },
    {
      done: hasProcessedPapers,
      label: 'Analyze a paper',
      description: 'Download, process, and summarize',
      actionLabel: 'Open Library',
      actionHref: '/feed?surface=library',
      disabled: !hasPapers,
    },
  ];

  return (
    <div className="space-y-8">
      <h1 className="text-[32px] leading-tight tracking-tight text-strong">Dashboard</h1>

      <SetupBanner />

      {showCelebration && (
        <Card className="rounded-md border-hair shadow-none">
          <CardContent className="flex items-center gap-3 pt-6">
            <CheckCircle2 className="h-5 w-5 shrink-0 text-[var(--status-ok)]" />
            <p className="font-medium">All set! Happy researching.</p>
          </CardContent>
        </Card>
      )}

      {showChecklist && (
        <>
          <Card className="rounded-md border-hair shadow-none">
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="text-xl">Welcome to JARVIS Research Assistant</CardTitle>
              <Button variant="ghost" size="icon" onClick={dismissChecklist} aria-label="Dismiss checklist">
                <X className="h-4 w-4" />
              </Button>
            </CardHeader>
            <CardContent>
              <p className="mb-4 text-sm text-muted-foreground">Get started in 3 steps:</p>
              <div className="space-y-4">
                {steps.map((step) => {
                  const StepIcon = step.done ? CheckCircle2 : Circle;
                  return (
                    <div key={step.label} className="flex items-start gap-3">
                      <StepIcon className={`mt-0.5 h-5 w-5 shrink-0 ${step.done ? 'text-[var(--status-ok)]' : 'text-muted-foreground'}`} />
                      <div className="flex-1">
                        <p className={`font-medium ${step.done ? 'line-through text-muted-foreground' : ''}`}>
                          {step.label}
                        </p>
                        <p className="text-sm text-muted-foreground">{step.description}</p>
                      </div>
                      {!step.done && !step.disabled && (
                        <Button variant="outline" size="sm" asChild>
                          <Link to={step.actionHref}>
                            {step.actionLabel} <ArrowRight className="ml-1 h-3 w-3" />
                          </Link>
                        </Button>
                      )}
                    </div>
                  );
                })}
              </div>
              <p className="mt-6 text-xs text-muted-foreground">
                Once you have papers, you can ask questions across your library, build citation
                and knowledge graphs, generate flashcards, and extract structured data.
              </p>
            </CardContent>
          </Card>
        </>
      )}

      <MetricTileGrid metrics={metrics} isLoading={isLoading} isError={isError} />

      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <CardTitle className="text-lg">Prepare library</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="mb-4 text-sm text-muted-foreground">
            Queue PDF processing and summaries for papers that are already in your library. New
            jobs appear in the jobs panel while they run.
          </p>
          <BatchButton
            label="Process PDFs"
            icon={Cog}
            mutationFn={() => batchProcessPapers(BATCH_LIMIT)}
            formatResult={(d) => (d.job_id ? `Queued ${d.queued} PDFs` : 'No PDFs to process')}
            onSuccessResult={(d) => trackBatchJob(d, 'papers.batch_process')}
            confirmMessage="This will queue PDF text extraction for papers that already have local PDFs. Continue?"
            confirmTitle="Process library PDFs?"
          />
          <div className="mt-4">
            <button
              type="button"
              className="flex items-center gap-1 text-sm text-muted-foreground"
              onClick={() => setAdvancedOpen((v) => !v)}
              aria-expanded={advancedOpen}
              aria-controls="batch-advanced"
            >
              {advancedOpen ? (
                <ChevronDown className="h-4 w-4 shrink-0" />
              ) : (
                <ChevronRight className="h-4 w-4 shrink-0" />
              )}
              Advanced
            </button>
            {advancedOpen && (
              <div id="batch-advanced" className="mt-3 flex flex-wrap gap-3">
                <BatchButton
                  label="Summarize"
                  icon={FileText}
                  mutationFn={() => batchSummarizePapers(BATCH_LIMIT)}
                  formatResult={(d) => (
                    d.job_id ? `Queued ${d.total_unsummarized} summaries` : 'No summaries to queue'
                  )}
                  onSuccessResult={(d) => trackBatchJob(d, 'papers.batch_summarize')}
                  confirmMessage="This will queue summaries for processed papers that do not have summaries yet. Continue?"
                />
                {isAdmin ? (
                  <BatchButton
                    label="Extract Entities"
                    icon={Sparkles}
                    mutationFn={batchExtractEntities}
                    formatResult={(d) => `Extracted ${d.extracted} papers`}
                    confirmMessage="This will extract entities from all summarized papers. Continue?"
                  />
                ) : (
                  <p className="basis-full text-xs text-muted-foreground">
                    Entity extraction is available to administrators after papers are summarized.
                  </p>
                )}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

    </div>
  );
}

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  CheckCircle2, Circle, ArrowRight, X,
  Cog, FileText, Sparkles, Loader2,
} from 'lucide-react';
import { fetchDashboardMetrics, batchProcessPapers, batchSummarizePapers, batchExtractEntities } from '@/lib/api';
import { MetricTileGrid } from '@/components/home/MetricTileGrid';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useUIStore } from '@/stores/ui-store';
import { SetupBanner } from '@/components/setup/SetupBanner';
import { SectionHeader } from '@/components/my-day/sections/SectionHeader';

interface BatchButtonProps<T> {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  mutationFn: () => Promise<T>;
  formatResult: (data: T) => string;
  confirmMessage?: string;
}

function BatchButton<T>({
  label,
  icon: Icon,
  mutationFn,
  formatResult,
  confirmMessage,
}: BatchButtonProps<T>) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboard-metrics'] });
    },
  });

  return (
    <div className="flex flex-col items-start gap-1">
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          if (confirmMessage && !window.confirm(confirmMessage)) return;
          mutation.mutate();
        }}
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
          Failed: {(mutation.error as Error).message}
        </span>
      )}
    </div>
  );
}

export function HomePage() {
  const { data: metrics, isLoading } = useQuery({
    queryKey: ['dashboard-metrics'],
    queryFn: fetchDashboardMetrics,
    refetchInterval: 60_000,
  });

  const { checklistDismissed, dismissChecklist } = useUIStore();

  const stage = metrics?.onboarding_stage ?? 'needs_topics';
  const hasTopics = stage !== 'needs_topics';
  const hasPapers = hasTopics && stage !== 'needs_papers';
  const hasProcessedPapers = hasPapers && stage === 'complete';

  const showChecklist = !checklistDismissed && metrics?.onboarding_stage !== 'complete' && !isLoading;

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
      actionLabel: 'Go to Feed',
      actionHref: '/feed',
      disabled: !hasTopics,
    },
    {
      done: hasProcessedPapers,
      label: 'Analyze a paper',
      description: 'Download, process, and summarize',
      actionLabel: 'Go to Feed',
      actionHref: '/feed',
      disabled: !hasPapers,
    },
  ];

  return (
    <div className="space-y-8">
      <h1 className="text-[32px] leading-tight tracking-tight text-strong">Dashboard</h1>

      <SetupBanner />

      {showChecklist && (
        <>
          <SectionHeader marker="GET STARTED" />
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

      <SectionHeader marker="QUICK STATS" />
      <MetricTileGrid metrics={metrics} isLoading={isLoading} />

      <SectionHeader marker="BATCH OPS" />
      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <CardTitle className="text-lg">Batch Operations</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="mb-4 text-sm text-muted-foreground">
            Process unanalyzed papers in bulk. Run in order: Process → Summarize → Extract.
          </p>
          <div className="flex flex-wrap gap-3">
            <BatchButton
              label="Process PDFs"
              icon={Cog}
              mutationFn={batchProcessPapers}
              formatResult={(d) => `Queued ${d.queued} papers`}
              confirmMessage="This will process PDFs for all papers in your library. This may take several minutes. Continue?"
            />
            <BatchButton
              label="Summarize"
              icon={FileText}
              mutationFn={batchSummarizePapers}
              formatResult={(d) => `Queued ${d.total_unsummarized} papers`}
              confirmMessage="This will generate AI summaries for all unprocessed papers. This costs LLM tokens. Continue?"
            />
            <BatchButton
              label="Extract Entities"
              icon={Sparkles}
              mutationFn={batchExtractEntities}
              formatResult={(d) => `Extracted ${d.extracted} papers`}
              confirmMessage="This will extract entities from all papers. This costs LLM tokens. Continue?"
            />
          </div>
        </CardContent>
      </Card>

    </div>
  );
}

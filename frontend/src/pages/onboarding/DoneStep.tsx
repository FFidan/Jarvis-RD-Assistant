import { useEffect, useRef } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { UseMutationResult } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, AlertTriangle, RefreshCw, CheckCircle2, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { SetupStep } from '@/components/setup/SetupStep';
import { SystemCheck } from '@/components/setup/SystemCheck';
import { markSetupCompleted } from '@/lib/api';
import { generatePulseNow } from '@/lib/api/pulse';
import { useJobStore } from '@/stores/job-store';
import { errorMessage } from '@/lib/errors';
import { markFirstRunCompleted } from './shared';

export function DoneStep({
  stepNumber,
  totalSteps,
  authed,
}: {
  stepNumber: number;
  totalSteps: number;
  authed: boolean;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const hasTriggered = useRef(false);

  const markMut: UseMutationResult<void, Error, void> = useMutation({
    mutationFn: markSetupCompleted,
    onSuccess: () => {
      markFirstRunCompleted(queryClient);
    },
    onError: (err: Error) => {
      console.error('Failed to mark setup completed', err);
    },
  });

  const pulseMut = useMutation({
    mutationFn: generatePulseNow,
    onSuccess: (res) => {
      useJobStore.getState().trackExternalJob({
        jobId: res.job_id,
        kind: 'pulse.generate',
        payload: { source: 'first_run' },
        status: res.status === 'running' ? 'running' : 'queued',
      });
    },
  });

  useEffect(() => {
    if (!hasTriggered.current) {
      hasTriggered.current = true;
      markMut.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (markMut.isError) {
    return (
      <SetupStep
        stepNumber={stepNumber}
        totalSteps={totalSteps}
        title="You're all set"
        description="JARVIS is ready to help with your research."
        footer={<span />}
      >
        <div className="space-y-4">
          <div className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-4">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
            <div className="space-y-1 text-sm">
              <p className="font-medium">Setup completion failed</p>
              <p className="text-muted-foreground">{errorMessage(markMut.error, 'Could not save setup status.')}</p>
            </div>
          </div>
          <Button
            onClick={() => {
              hasTriggered.current = false;
              markMut.reset();
              markMut.mutate();
              hasTriggered.current = true;
            }}
          >
            <RefreshCw className="mr-2 h-4 w-4" />
            Retry
          </Button>
        </div>
      </SetupStep>
    );
  }

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="You're all set"
      description="JARVIS is ready to help with your research."
      footer={
        <>
          <span />
          {markMut.isPending ? (
            <Button disabled>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Finishing…
            </Button>
          ) : (
            <Button
              onClick={() => {
                markFirstRunCompleted(queryClient);
                navigate('/', { replace: true });
              }}
            >
              Go to dashboard
              <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          )}
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border border-green-500/40 bg-green-500/10 p-4">
        <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-green-500" />
        <div className="space-y-1 text-sm">
          <p className="font-medium">Setup complete</p>
          <p className="text-muted-foreground">
            You can revisit any of these settings from the Settings page. Integrations like Telegram
            live under Settings &rarr; Integrations.
          </p>
        </div>
      </div>
      {authed && (
        <div className="space-y-4">
          <div className="rounded-md border border-border bg-muted/30 p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="space-y-1">
                <p className="text-sm font-medium">Start discovery</p>
                <p className="text-sm text-muted-foreground">
                  Build your first Pulse from configured paper sources before the dashboard feels empty.
                </p>
              </div>
              <Button
                variant="outline"
                onClick={() => pulseMut.mutate()}
                disabled={pulseMut.isPending || pulseMut.isSuccess}
              >
                {pulseMut.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="mr-2 h-4 w-4" />
                )}
                {pulseMut.isSuccess ? 'Discovery queued' : 'Discover papers now'}
              </Button>
            </div>
            {pulseMut.isError && (
              <p className="mt-2 text-sm text-destructive">
                Discovery could not start: {errorMessage(pulseMut.error, 'try again from the dashboard.')}
              </p>
            )}
          </div>
          <div className="space-y-2">
            <p className="text-sm font-medium text-muted-foreground">Setup readiness</p>
            <SystemCheck />
          </div>
        </div>
      )}
    </SetupStep>
  );
}

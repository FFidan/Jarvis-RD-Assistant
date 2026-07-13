import { useEffect, useState } from 'react';
import { ArrowRight, RefreshCw, Sparkles, AlertTriangle, Loader2, CheckCircle2, Cpu } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { SetupStep } from '@/components/setup/SetupStep';
import { runFirstRunSystemCheck, type FirstRunSystemCheck, type FirstRunStatus } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import type { StepNavProps } from './shared';

interface WelcomeSystemCheckStepProps extends StepNavProps {
  onNext: () => void;
  onSkip: () => void;
  skipError?: Error | null;
  setupToken?: string | null;
  /** Already-fetched pre-auth status — reused here (no new fetch) to surface
   * the detected hardware tier while the models row may still be warming up. */
  firstRun?: FirstRunStatus;
}

export function WelcomeSystemCheckStep({
  stepNumber,
  totalSteps,
  onNext,
  onSkip,
  skipError,
  setupToken,
  firstRun,
}: WelcomeSystemCheckStepProps) {
  const [data, setData] = useState<FirstRunSystemCheck | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await runFirstRunSystemCheck(setupToken));
    } catch (e) {
      setError(errorMessage(e, 'Probe failed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Welcome to JARVIS"
      description="Let&apos;s make sure every backing service is reachable. The models row may still be warming up — that never blocks Continue."
      footer={
        <>
          <Button variant="ghost" onClick={onSkip}>
            Skip setup
          </Button>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={() => void run()} disabled={loading}>
              <RefreshCw className="mr-2 h-4 w-4" /> Re-check
            </Button>
            <Button onClick={onNext}>
              Continue <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border p-4">
        <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
        <div className="space-y-1 text-sm">
          <p>
            JARVIS is a self-hosted research assistant: paper discovery, chat with your saved
            papers, spaced-repetition flashcards, and project tracking.
          </p>
          <p className="text-muted-foreground">
            This wizard checks your environment, creates your admin account, sets up a topic and
            the nightly Pulse schedule, and (optionally) pairs a Telegram bot.
          </p>
        </div>
      </div>
      {firstRun?.hw_tier_current && (
        <div className="flex items-start gap-3 rounded-md border p-4" data-testid="detected-hardware">
          <Cpu className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">
            Detected hardware tier{' '}
            <span className="font-mono font-medium text-foreground">{firstRun.hw_tier_current}</span>
            {firstRun.recommended_backend && (
              <>
                {' '}
                — recommended backend{' '}
                <span className="font-mono font-medium text-foreground">
                  {firstRun.recommended_backend}
                </span>
              </>
            )}
            {firstRun.gpu_vendor && firstRun.gpu_vendor !== 'none' && (
              <>
                {' '}
                (<span className="font-mono font-medium text-foreground">{firstRun.gpu_vendor}</span> GPU)
              </>
            )}
            . You can review and change models later from Settings &rarr; Models.
          </p>
        </div>
      )}
      {loading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Probing services...
        </div>
      )}
      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-destructive" />
          <span>{error}</span>
        </div>
      )}
      {skipError && (
        <p className="text-sm text-destructive">{errorMessage(skipError, 'Could not skip setup — try again.')}</p>
      )}
      {data && (
        <ul className="space-y-2">
          {data.services.map((svc) => (
            <li
              key={svc.name}
              className="flex items-center justify-between rounded-md border p-3 text-sm"
              data-testid={`svc-${svc.name}`}
            >
              <span className="font-medium capitalize">{svc.name}</span>
              {svc.ok ? (
                <span className="flex items-center gap-1 text-green-600">
                  <CheckCircle2 className="h-4 w-4" /> Ready
                </span>
              ) : (
                <span className="flex items-center gap-1 text-destructive" title={svc.detail ?? ''}>
                  <AlertTriangle className="h-4 w-4" /> {svc.detail ?? 'Unavailable'}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {data && !data.all_ok && (
        <p className="text-xs text-muted-foreground">
          Some services are still warming up — that&apos;s expected on first boot. You can continue and
          re-check later from Settings &rarr; System.
        </p>
      )}
    </SetupStep>
  );
}

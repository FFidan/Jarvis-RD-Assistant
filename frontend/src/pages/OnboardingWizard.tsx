import { useEffect, useRef } from 'react';
import { useSearchParams, useNavigate, useLocation } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { markSetupCompleted, type FirstRunStatus } from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import {
  deriveSteps,
  readStoredSetupToken,
  readSetupTokenFromHash,
  storeSetupToken,
  markFirstRunCompleted,
  type StepKind,
} from './onboarding/shared';
import { WelcomeSystemCheckStep } from './onboarding/WelcomeStep';
import { SmtpStep } from './onboarding/SmtpStep';
import { AdminStep } from './onboarding/AdminStep';
import { CloudLlmStep } from './onboarding/CloudLlmStep';
import { FirstTopicStep } from './onboarding/FirstTopicStep';
import { AutomationStep } from './onboarding/AutomationStep';
import { SourceApiKeysStep } from './onboarding/SourceApiKeysStep';
import { TelegramStep } from './onboarding/TelegramStep';
import { DoneStep } from './onboarding/DoneStep';

interface OnboardingWizardProps {
  firstRun: FirstRunStatus;
  authed: boolean;
}

export function isRemotePlainHttp(
  location: Pick<Location, 'protocol' | 'hostname'>,
): boolean {
  const loopback = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);
  return location.protocol === 'http:' && !loopback.has(location.hostname.toLowerCase());
}

function RemoteHttpSetupBlock() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="w-full max-w-lg rounded-lg border bg-card p-6">
        <h1 className="text-xl font-semibold">Use a private HTTPS address to finish setup</h1>
        <p className="mt-3 text-sm text-muted-foreground">
          This plain-HTTP LAN address is for status checks only. Do not enter a setup token,
          sign-in link, or API key here.
        </p>
        <p className="mt-3 text-sm text-muted-foreground">
          On the server, open the localhost setup link printed by <code>./setup.sh</code>. For
          another device, finish the Tailscale or named-HTTPS step and use the verified address
          it prints.
        </p>
      </div>
    </main>
  );
}

export function OnboardingWizard(props: OnboardingWizardProps) {
  if (isRemotePlainHttp(window.location)) {
    return <RemoteHttpSetupBlock />;
  }
  return <OnboardingWizardContent {...props} />;
}

function OnboardingWizardContent({ firstRun, authed }: OnboardingWizardProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // The setup token arrives as a URL fragment (`#setup_token=…`) so the bearer
  // never reaches the request line / server access logs; the `?setup_token=`
  // query form stays supported for links printed before the fragment migration.
  const setupTokenRef = useRef<string | null>(
    readSetupTokenFromHash(location.hash) ?? searchParams.get('setup_token') ?? readStoredSetupToken(),
  );

  useEffect(() => {
    const urlToken = readSetupTokenFromHash(location.hash) ?? searchParams.get('setup_token');
    if (!urlToken) return;
    storeSetupToken(urlToken);
    // Strip the token from the address bar — setSearchParams replaces both the
    // query string and the fragment — while preserving the wizard step.
    setSearchParams({ step: searchParams.get('step') ?? '1' }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const showAdminStep = !firstRun.configured;
  const role = useAuthStore((s) => s.user?.role);
  // First run creates the first admin, so an unauthenticated wizard is admin-to-be.
  const canManageTopics = !authed || role === 'admin';
  const steps = deriveSteps(firstRun, { canManageTopics });
  const totalSteps = steps.length;

  const clampStep = (raw: string | null): number => {
    const n = raw ? parseInt(raw, 10) : 1;
    if (Number.isNaN(n) || n < 1) return 1;
    if (n > totalSteps) return totalSteps;
    return n;
  };

  const step = clampStep(searchParams.get('step'));
  const kind = steps[step - 1] ?? 'welcome';

  const goToStep = (n: number) => {
    const clamped = Math.max(1, Math.min(totalSteps, n));
    setSearchParams({ step: String(clamped) });
  };
  const goNext = () => goToStep(step + 1);
  const goBack = () => goToStep(step - 1);

  const markCompletedMut = useMutation({
    mutationFn: markSetupCompleted,
    onSuccess: () => {
      markFirstRunCompleted(queryClient);
      navigate('/', { replace: true });
    },
    onError: (err: Error) => {
      console.error('Failed to mark setup completed', err);
    },
  });

  const handleSkipAll = () => {
    if (!authed) {
      const targetKind: StepKind = showAdminStep ? 'admin' : 'cloud';
      const targetIdx = steps.indexOf(targetKind);
      goToStep(targetIdx >= 0 ? targetIdx + 1 : 1);
      return;
    }
    markCompletedMut.mutate();
  };

  const stepProps = { stepNumber: step, totalSteps };

  return (
    <div className="min-h-screen bg-background">
      {kind === 'welcome' && (
        <WelcomeSystemCheckStep
          {...stepProps}
          onNext={goNext}
          onSkip={handleSkipAll}
          skipError={markCompletedMut.isError ? markCompletedMut.error : null}
          setupToken={setupTokenRef.current}
          firstRun={firstRun}
        />
      )}
      {kind === 'smtp' && (
        <SmtpStep
          {...stepProps}
          onBack={goBack}
          onNext={goNext}
          singleUser={firstRun.setup_mode === 'single'}
          setupToken={setupTokenRef.current}
        />
      )}
      {kind === 'admin' && (
        <AdminStep {...stepProps} onBack={goBack} onNext={goNext} setupToken={setupTokenRef.current} />
      )}
      {kind === 'cloud' && (
        <CloudLlmStep {...stepProps} onBack={goBack} onNext={goNext} setupToken={setupTokenRef.current} />
      )}
      {kind === 'topic' && <FirstTopicStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'automation' && <AutomationStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'sources' && <SourceApiKeysStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'telegram' && <TelegramStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'done' && <DoneStep {...stepProps} authed={authed} />}
    </div>
  );
}

export default OnboardingWizard;

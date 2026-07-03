import { useEffect, useRef } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { markSetupCompleted, type FirstRunStatus } from '@/lib/api';
import {
  ALL_STEPS,
  SINGLE_USER_FIRST_RUN_STEPS,
  readStoredSetupToken,
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

export function OnboardingWizard({ firstRun, authed }: OnboardingWizardProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const setupTokenRef = useRef<string | null>(
    searchParams.get('setup_token') ?? readStoredSetupToken(),
  );

  useEffect(() => {
    const urlToken = searchParams.get('setup_token');
    if (!urlToken) return;
    storeSetupToken(urlToken);
    setSearchParams({ step: searchParams.get('step') ?? '1' }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const showAdminStep = !firstRun.configured;
  const baseSteps =
    showAdminStep && firstRun.setup_mode === 'single' ? SINGLE_USER_FIRST_RUN_STEPS : ALL_STEPS;
  const steps = showAdminStep ? baseSteps : ALL_STEPS.filter((s) => s !== 'admin');
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

import { useQuery } from '@tanstack/react-query';
import { ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { SetupStep } from '@/components/setup/SetupStep';
import { TelegramPairingSection } from '@/components/settings/TelegramPairingSection';
import { getTelegramPairing } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import type { StepNavProps } from './shared';

interface TelegramStepProps extends StepNavProps {
  onBack: () => void;
  onNext: () => void;
}

export function TelegramStep({ stepNumber, totalSteps, onBack, onNext }: TelegramStepProps) {
  const { data } = useQuery({
    queryKey: QUERY_KEYS.pairing.userTelegram(),
    queryFn: getTelegramPairing,
  });
  const paired = data?.paired === true;

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Pair Telegram (optional)"
      description="Receive daily briefings and interact with JARVIS from your phone."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {paired ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <TelegramPairingSection />
    </SetupStep>
  );
}

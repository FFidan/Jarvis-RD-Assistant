import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, CheckCircle2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { TimeSelect } from '@/components/ui/time-select';
import { SetupStep } from '@/components/setup/SetupStep';
import { getConfigValue } from '@/components/settings/pulse/pulse-utils';
import { fetchConfig, setConfig } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { errorMessage } from '@/lib/errors';
import { cronToHumanReadable, cronToTime, isTimeOnlyCron, timeToCron } from '@/lib/cron-utils';
import type { StepNavProps } from './shared';

interface AutomationStepProps extends StepNavProps {
  onBack: () => void;
  onNext: () => void;
}

export function AutomationStep({ stepNumber, totalSteps, onBack, onNext }: AutomationStepProps) {
  const queryClient = useQueryClient();

  const { data: configs } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
    staleTime: 30_000,
  });
  const persistedCron = configs ? getConfigValue<string | null>(configs, 'pulse.cron', null) : null;
  const persistedEnabled = configs ? getConfigValue<boolean | null>(configs, 'pulse.enabled', null) : null;

  const [time, setTime] = useState('04:00');
  const [pulseEnabled, setPulseEnabled] = useState(true);
  const [saved, setSaved] = useState(false);
  const [partialWarning, setPartialWarning] = useState<string | null>(null);
  const seededRef = useRef(false);

  useEffect(() => {
    if (seededRef.current || !configs) return;
    seededRef.current = true;
    if (persistedCron) setTime(cronToTime(persistedCron));
    if (persistedEnabled !== null) setPulseEnabled(persistedEnabled);
    if (persistedCron !== null || persistedEnabled !== null) setSaved(true);
  }, [configs, persistedCron, persistedEnabled]);

  const saveMut = useMutation({
    mutationFn: async (value: string) => {
      await setConfig('pulse.cron', value);
      try {
        await setConfig('pulse.enabled', pulseEnabled);
        return { partial: null as string | null };
      } catch {
        return {
          partial: 'The schedule time was saved, but enabling Pulse failed — try the switch again.',
        };
      }
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      setSaved(result.partial === null);
      setPartialWarning(result.partial);
    },
    onError: (err: Error) => {
      console.error('Failed to save pulse config', err);
    },
  });

  const handleSave = () => {
    // Pass the stored expression so a schedule the clock picker cannot
    // represent (multiple daily runs, hourly, etc.) is preserved rather than
    // silently collapsed to a single daily time.
    saveMut.mutate(timeToCron(time, persistedCron));
  };

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Automation schedule"
      description="Pulse will run daily at this time to discover new papers."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {saved ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={pulseEnabled}
            onChange={(e) => setPulseEnabled(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300"
            aria-label="Enable Pulse"
          />
          <span className="text-sm font-medium">Enable Pulse (overnight paper discovery)</span>
        </label>
        {persistedCron !== null && !isTimeOnlyCron(persistedCron) ? (
          <p className="text-sm text-muted-foreground">
            Pulse already has a schedule set: {cronToHumanReadable(persistedCron)}. This step can
            only set a single daily time — change the full schedule later in Settings.
          </p>
        ) : (
          <div>
            <Label htmlFor="setup-pulse-time">Daily run time</Label>
            <TimeSelect value={time} onChange={setTime} disabled={!pulseEnabled} />
          </div>
        )}
        <div className="flex items-center gap-3">
          <Button onClick={handleSave} disabled={saveMut.isPending}>
            Save schedule
          </Button>
          {saved && (
            <span className="flex items-center gap-1 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              Saved
            </span>
          )}
        </div>
        {saveMut.isError && (
          <p className="text-sm text-destructive">{errorMessage(saveMut.error, 'Could not save schedule — try again.')}</p>
        )}
        {partialWarning && <p className="text-sm text-amber-600">{partialWarning}</p>}
      </div>
    </SetupStep>
  );
}

import { useEffect, useRef, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { AlertTriangle, ArrowRight, CheckCircle2, Loader2, RefreshCw, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { TimeSelect } from '@/components/ui/time-select';
import { Textarea } from '@/components/ui/textarea';
import { createTopic, markSetupCompleted, setConfig } from '@/lib/api';
import { SetupStep } from '@/components/setup/SetupStep';
import { SystemCheck } from '@/components/setup/SystemCheck';
import { PairTelegram } from '@/components/setup/PairTelegram';
import { SourceApiKeysStep } from '@/components/setup/SourceApiKeysStep';
import { timeToCron } from '@/lib/cron-utils';
import type { Topic } from '@/types';

const TOTAL_STEPS = 7;

function clampStep(raw: string | null): number {
  const n = raw ? parseInt(raw, 10) : 1;
  if (Number.isNaN(n) || n < 1) return 1;
  if (n > TOTAL_STEPS) return TOTAL_STEPS;
  return n;
}

export function SetupWizard() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const step = clampStep(searchParams.get('step'));

  const goToStep = (n: number) => {
    const clamped = Math.max(1, Math.min(TOTAL_STEPS, n));
    setSearchParams({ step: String(clamped) });
  };

  const markCompletedMut = useMutation({
    mutationFn: markSetupCompleted,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.setup.status() });
    },
    onError: (err: Error) => {
      console.error('Failed to mark setup completed', err);
    },
  });

  const handleSkipAll = () => {
    markCompletedMut.mutate(undefined, {
      onSuccess: () => navigate('/'),
    });
  };

  return (
    <div className="min-h-screen bg-background">
      {step === 1 && <WelcomeStep onNext={() => goToStep(2)} onSkip={handleSkipAll} />}
      {step === 2 && (
        <SystemCheckStep onBack={() => goToStep(1)} onNext={() => goToStep(3)} />
      )}
      {step === 3 && (
        <FirstTopicStep onBack={() => goToStep(2)} onNext={() => goToStep(4)} />
      )}
      {step === 4 && (
        <AutomationStep onBack={() => goToStep(3)} onNext={() => goToStep(5)} />
      )}
      {step === 5 && (
        <SourceApiKeysStep onBack={() => goToStep(4)} onNext={() => goToStep(6)} />
      )}
      {step === 6 && (
        <TelegramStep onBack={() => goToStep(5)} onNext={() => goToStep(7)} />
      )}
      {step === 7 && <DoneStep />}
    </div>
  );
}

// --- Step 1: Welcome ---

function WelcomeStep({ onNext, onSkip }: { onNext: () => void; onSkip: () => void }) {
  return (
    <SetupStep
      stepNumber={1}
      totalSteps={TOTAL_STEPS}
      title="Welcome to JARVIS"
      description="Let's set up JARVIS in 7 steps. You can skip optional steps at any time."
      footer={
        <>
          <Button variant="ghost" onClick={onSkip}>
            Skip setup
          </Button>
          <Button onClick={onNext}>
            Get started
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border p-4">
        <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
        <div className="space-y-1 text-sm">
          <p>
            JARVIS is a self-hosted research assistant: paper discovery, RAG chat over your
            library, spaced-repetition flashcards, and project tracking.
          </p>
          <p className="text-muted-foreground">
            This wizard will check your environment, set up a topic, configure the nightly
            Pulse schedule, and (optionally) pair a Telegram bot for push notifications.
          </p>
        </div>
      </div>
    </SetupStep>
  );
}

// --- Step 2: System check ---

function SystemCheckStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  return (
    <SetupStep
      stepNumber={2}
      totalSteps={TOTAL_STEPS}
      title="System check"
      description="We'll poll every few seconds while models download."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            Next
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <SystemCheck />
    </SetupStep>
  );
}

// --- Step 3: First topic ---

function FirstTopicStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [added, setAdded] = useState(false);

  const createMut = useMutation({
    mutationFn: (data: Partial<Topic>) => createTopic(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.list() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.setup.status() });
      setAdded(true);
      setName('');
      setDescription('');
    },
    onError: (err: Error) => {
      console.error('Failed to create topic', err);
    },
  });

  const handleAdd = () => {
    if (!name.trim()) return;
    createMut.mutate({
      name: name.trim(),
      query_terms: [name.trim()],
      description: description.trim() || null,
    } as Partial<Topic>);
  };

  return (
    <SetupStep
      stepNumber={3}
      totalSteps={TOTAL_STEPS}
      title="Your first research topic"
      description="Topics drive paper discovery. You can add more later in Settings."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {added ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <Label htmlFor="setup-topic-name">Topic name</Label>
          <Input
            id="setup-topic-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Neural ODEs"
          />
        </div>
        <div>
          <Label htmlFor="setup-topic-description">Description (optional)</Label>
          <Textarea
            id="setup-topic-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Context for the Pulse scoring LLM"
            rows={3}
            maxLength={1000}
          />
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={handleAdd} disabled={!name.trim() || createMut.isPending}>
            Add topic
          </Button>
          {added && (
            <span className="flex items-center gap-1 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              Topic added
            </span>
          )}
        </div>
      </div>
    </SetupStep>
  );
}

// --- Step 4: Automation schedule ---

function AutomationStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const queryClient = useQueryClient();
  const [time, setTime] = useState('04:00');
  const [pulseEnabled, setPulseEnabled] = useState(true);
  const [saved, setSaved] = useState(false);

  const saveMut = useMutation({
    mutationFn: async (value: string) => {
      await setConfig('pulse.cron', value);
      await setConfig('pulse.enabled', pulseEnabled);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      setSaved(true);
    },
    onError: (err: Error) => {
      console.error('Failed to save pulse config', err);
    },
  });

  const handleSave = () => {
    saveMut.mutate(timeToCron(time));
  };

  return (
    <SetupStep
      stepNumber={4}
      totalSteps={TOTAL_STEPS}
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
        <div>
          <Label htmlFor="setup-pulse-time">Daily run time</Label>
          <TimeSelect value={time} onChange={setTime} disabled={!pulseEnabled} />
          <p className="mt-1 text-xs text-muted-foreground">
            Equivalent cron: <span className="font-mono">{timeToCron(time)}</span>
          </p>
        </div>
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
      </div>
    </SetupStep>
  );
}

// --- Step 6: Telegram ---

function TelegramStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  return (
    <SetupStep
      stepNumber={6}
      totalSteps={TOTAL_STEPS}
      title="Pair Telegram (optional)"
      description="Receive daily briefings and interact with JARVIS from your phone."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            Skip for now
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <PairTelegram onPaired={onNext} />
    </SetupStep>
  );
}

// --- Step 7: Done ---

function DoneStep() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const hasTriggered = useRef(false);

  const markMut = useMutation({
    mutationFn: markSetupCompleted,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.setup.status() });
      navigate('/');
    },
    onError: (err: Error) => {
      console.error('Failed to mark setup completed', err);
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
        stepNumber={6}
        totalSteps={TOTAL_STEPS}
        title="You're all set"
        description="JARVIS is ready to help with your research."
        footer={<span />}
      >
        <div className="space-y-4">
          <div className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-4">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
            <div className="space-y-1 text-sm">
              <p className="font-medium">Setup completion failed</p>
              <p className="text-muted-foreground">
                Could not save setup status. Please try again.
              </p>
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
      stepNumber={7}
      totalSteps={TOTAL_STEPS}
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
            <Button onClick={() => navigate('/')}>
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
            You can revisit any of these settings from the Settings page. Integrations like
            Telegram live under Settings - Integrations.
          </p>
        </div>
      </div>
    </SetupStep>
  );
}

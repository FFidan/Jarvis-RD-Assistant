import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SetupStep } from '@/components/setup/SetupStep';
import { saveFirstRunCloudKeys, type FirstRunCloudKeysBody } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import type { StepNavProps } from './shared';

interface CloudLlmStepProps extends StepNavProps {
  onBack: () => void;
  onNext: () => void;
  setupToken?: string | null;
}

export function CloudLlmStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
  setupToken,
}: CloudLlmStepProps) {
  const [openai, setOpenai] = useState('');
  const [anthropic, setAnthropic] = useState('');
  const [gemini, setGemini] = useState('');

  const saveMut = useMutation({
    mutationFn: (body: FirstRunCloudKeysBody) => saveFirstRunCloudKeys(body, setupToken),
    onSuccess: () => onNext(),
    onError: (err: Error) => {
      console.error('Failed to save cloud LLM keys', err);
    },
  });

  const anyEntered = openai || anthropic || gemini;

  function providerSaveStatus(provider: string): string | null {
    if (!saveMut.isSuccess || !saveMut.data) return null;
    const { applied_now, restart_required } = saveMut.data;
    if (applied_now.includes(provider)) return 'Applied now';
    if (restart_required) return 'Saved — applies after restart';
    return 'Saved';
  }

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Cloud LLM keys (optional)"
      description="Skip if you only want to run on local Ollama models."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={onNext}>
              Skip
            </Button>
            <Button
              onClick={() =>
                saveMut.mutate({ openai: openai || null, anthropic: anthropic || null, gemini: gemini || null })
              }
              disabled={!anyEntered || saveMut.isPending}
            >
              Save & continue
            </Button>
          </div>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-openai">OpenAI API key</Label>
            {openai && providerSaveStatus('openai') && (
              <span className="text-xs text-green-600">{providerSaveStatus('openai')}</span>
            )}
          </div>
          <Input id="cloud-openai" type="password" value={openai} onChange={(e) => setOpenai(e.target.value)} placeholder="sk-..." autoComplete="off" />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-anthropic">Anthropic API key</Label>
            {anthropic && providerSaveStatus('anthropic') && (
              <span className="text-xs text-green-600">{providerSaveStatus('anthropic')}</span>
            )}
          </div>
          <Input id="cloud-anthropic" type="password" value={anthropic} onChange={(e) => setAnthropic(e.target.value)} placeholder="sk-ant-..." autoComplete="off" />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-gemini">Google Gemini API key</Label>
            {gemini && providerSaveStatus('gemini') && (
              <span className="text-xs text-green-600">{providerSaveStatus('gemini')}</span>
            )}
          </div>
          <Input id="cloud-gemini" type="password" value={gemini} onChange={(e) => setGemini(e.target.value)} autoComplete="off" />
        </div>
      </div>
      {saveMut.isError && (
        <p className="text-sm text-destructive">{errorMessage(saveMut.error, 'Could not save keys — try again.')}</p>
      )}
    </SetupStep>
  );
}

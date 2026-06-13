/**
 * PulseAdvancedTuningCard — collapsible card owning signal weights, discovery seed balance,
 * and L2 negative-feedback penalty.
 */
import { useState, useMemo } from 'react';
import type { UseMutationResult } from '@tanstack/react-query';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Slider } from '@/components/ui/slider';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { toast } from 'sonner';
import {
  DEFAULT_PULSE_WEIGHTS,
  CORE_SIGNAL_KEYS,
  OPTIONAL_SIGNAL_KEYS,
  PULSE_WEIGHT_KEYS,
  PULSE_WEIGHT_LABELS,
  PULSE_WEIGHT_TOOLTIPS,
  CONDITIONAL_SIGNAL_GATES,
  WEIGHT_PRESETS,
  type PulseWeightKey,
} from './pulse-constants';
import { useSyncedState } from './use-synced-state';
import { useDebouncedConfig } from './use-debounced-config';
import { getConfigValue } from './pulse-utils';
import { errorMessage } from '@/lib/errors';
import type { ConfigEntry } from '@/types';

function coerceWeights(raw: unknown): Record<PulseWeightKey, number> {
  const out = { ...DEFAULT_PULSE_WEIGHTS };
  if (raw && typeof raw === 'object') {
    for (const key of PULSE_WEIGHT_KEYS) {
      const value = (raw as Record<string, unknown>)[key];
      if (typeof value === 'number' && Number.isFinite(value)) {
        out[key] = value;
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Weight slider row
// ---------------------------------------------------------------------------

interface WeightSliderRowProps {
  weightKey: PulseWeightKey;
  value: number;
  disabled: boolean;
  capabilityPresent: boolean;
  onChange: (key: PulseWeightKey, value: number) => void;
}

function WeightSliderRow({
  weightKey,
  value,
  disabled,
  capabilityPresent,
  onChange,
}: WeightSliderRowProps) {
  const gate = CONDITIONAL_SIGNAL_GATES[weightKey];
  const sliderInput = (
    <input
      type="range"
      aria-label={`${PULSE_WEIGHT_LABELS[weightKey]} weight`}
      data-testid={`weight-slider-${weightKey}`}
      min={0}
      max={1}
      step={0.05}
      value={value}
      onChange={(e) => onChange(weightKey, Number(e.target.value))}
      disabled={disabled}
      className="w-full accent-primary"
    />
  );

  const sliderWithGate =
    gate && !capabilityPresent ? (
      <TooltipProvider delayDuration={150}>
        <Tooltip>
          <TooltipTrigger asChild>
            <div data-testid={`gate-tooltip-trigger-${weightKey}`}>{sliderInput}</div>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs text-xs">
            {gate.message}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    ) : (
      sliderInput
    );

  return (
    <div className="space-y-1">
      <Label className="flex items-center justify-between text-xs">
        <span className="flex items-center gap-1">
          {PULSE_WEIGHT_LABELS[weightKey]}
          <InfoTooltip content={PULSE_WEIGHT_TOOLTIPS[weightKey]} />
        </span>
        <span className="font-mono text-muted-foreground">{value.toFixed(2)}</span>
      </Label>
      {sliderWithGate}
    </div>
  );
}

// ---------------------------------------------------------------------------
// PulseAdvancedTuningCard
// ---------------------------------------------------------------------------

interface PulseAdvancedTuningCardProps {
  configs: ConfigEntry[];
  setMut: UseMutationResult<unknown, Error, { key: string; value: unknown }>;
  settingsControlsDisabled: boolean;
  hasNetworkx: boolean;
  hasSklearn: boolean;
}

export function PulseAdvancedTuningCard({
  configs,
  setMut,
  settingsControlsDisabled,
  hasNetworkx,
  hasSklearn,
}: PulseAdvancedTuningCardProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const recommendationEnabled = getConfigValue<boolean>(configs, 'recommendation.enabled', true);
  const likedWeightConfig = getConfigValue<number>(configs, 'recommendation.liked_weight', 0.6);
  const projectWeightConfig = getConfigValue<number>(configs, 'recommendation.project_weight', 0.4);
  const l2LambdaConfig = getConfigValue<number>(configs, 'pulse.l2_lambda', 0.5);
  const rawWeightsEntry = configs.find((c) => c.key === 'pulse.weights');
  const pulseWeightsServer = useMemo(
    () => coerceWeights(rawWeightsEntry?.value),
     
    [rawWeightsEntry?.value],
  );

  const [localLikedWeight, setLocalLikedWeight] = useSyncedState(likedWeightConfig);
  const [localProjectWeight, setLocalProjectWeight] = useSyncedState(projectWeightConfig);
  const [l2Lambda, setL2Lambda] = useSyncedState(l2LambdaConfig);
  const [localPulseWeights, setLocalPulseWeights] =
    useSyncedState<Record<PulseWeightKey, number>>(pulseWeightsServer);

  const debouncedWeights = useDebouncedConfig(
    ({ value }) => setMut.mutate({ key: 'pulse.weights', value }),
    400,
  );

  const updatePulseWeight = (key: PulseWeightKey, value: number) => {
    if (settingsControlsDisabled) return;
    const next = { ...localPulseWeights, [key]: value };
    setLocalPulseWeights(next);
    debouncedWeights({ key: 'pulse.weights', value: next });
  };

  const applyPreset = (preset: (typeof WEIGHT_PRESETS)[number]) => {
    if (settingsControlsDisabled) return;
    const next: Record<PulseWeightKey, number> = Object.assign(
      { ...DEFAULT_PULSE_WEIGHTS },
      preset.weights,
    );
    setLocalPulseWeights(next);
    setMut.mutate({ key: 'pulse.weights', value: next });
  };

  const pulseWeightSum = PULSE_WEIGHT_KEYS.reduce((acc, k) => acc + localPulseWeights[k], 0);

  const handleNormalize = () => {
    if (settingsControlsDisabled || pulseWeightSum === 0) return;
    const scale = 1 / pulseWeightSum;
    const next = { ...localPulseWeights };
    PULSE_WEIGHT_KEYS.forEach((k) => {
      next[k] = Math.round(localPulseWeights[k] * scale * 100) / 100;
    });
    setLocalPulseWeights(next);
    setMut.mutate({ key: 'pulse.weights', value: next });
  };

  const capabilityForKey = (key: PulseWeightKey): boolean => {
    const gate = CONDITIONAL_SIGNAL_GATES[key];
    if (!gate) return true;
    if (gate.capability === 'networkx') return hasNetworkx;
    if (gate.capability === 'scikit_learn') return hasSklearn;
    return true;
  };

  return (
    <Card className="rounded-md border-hair shadow-none" data-testid="pulse-weights-card">
      <CardHeader className="pb-3">
        <button
          type="button"
          className="flex w-full items-start gap-2 text-left"
          onClick={() => setAdvancedOpen((v) => !v)}
          aria-expanded={advancedOpen}
          aria-controls="pulse-advanced-tuning"
        >
          {advancedOpen ? (
            <ChevronDown className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <ChevronRight className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <div className="space-y-1">
            <CardTitle className="text-base">Advanced tuning</CardTitle>
            <CardDescription>
              Signal weights, recommender seed balance, and negative-feedback penalty.
            </CardDescription>
          </div>
        </button>
      </CardHeader>

      {advancedOpen && (
        <CardContent id="pulse-advanced-tuning" className="space-y-5 border-t pt-4">
          <div className="space-y-4">
            <div>
              <h4 className="text-sm font-medium">Scoring weights</h4>
              <p className="text-xs text-muted-foreground">
                Weights applied to each signal when ranking Pulse candidate papers.
                Values should roughly sum to 1.0.
              </p>
            </div>

            {/* Presets */}
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground">Presets</p>
              <div className="flex flex-wrap gap-2" data-testid="weight-presets">
                {WEIGHT_PRESETS.map((preset) => (
                  <TooltipProvider key={preset.label} delayDuration={200}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-7 px-2.5 text-xs"
                          onClick={() => applyPreset(preset)}
                          disabled={settingsControlsDisabled}
                          data-testid={`preset-${preset.label.toLowerCase().replace(/\s+/g, '-')}`}
                        >
                          {preset.label}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="bottom" className="max-w-xs text-xs">
                        {preset.description}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                ))}
              </div>
            </div>

            {/* Core signal sliders */}
            <div className="space-y-3">
              {CORE_SIGNAL_KEYS.map((key) => (
                <WeightSliderRow
                  key={key}
                  weightKey={key}
                  value={localPulseWeights[key]}
                  disabled={settingsControlsDisabled}
                  capabilityPresent={true}
                  onChange={updatePulseWeight}
                />
              ))}
            </div>

            {/* Optional signal sliders */}
            <div className="space-y-3 rounded-md border border-dashed p-3">
              <div>
                <p className="text-xs font-semibold">
                  Optional signals — need extra data or dependencies
                </p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  These signals are inactive by default. Enable them once the prerequisites are in
                  place.
                </p>
              </div>
              {OPTIONAL_SIGNAL_KEYS.map((key) => (
                <WeightSliderRow
                  key={key}
                  weightKey={key}
                  value={localPulseWeights[key]}
                  disabled={settingsControlsDisabled}
                  capabilityPresent={capabilityForKey(key)}
                  onChange={updatePulseWeight}
                />
              ))}
            </div>

            {/* Sum readout + normalize */}
            <div className="flex items-center gap-2">
              <p
                className={`text-xs ${
                  Math.abs(pulseWeightSum - 1.0) > 0.2
                    ? 'text-[var(--status-warn)]'
                    : 'text-muted-foreground'
                }`}
              >
                Sum: {pulseWeightSum.toFixed(2)}
                {Math.abs(pulseWeightSum - 1.0) > 0.2 && ' (target ~1.0)'}
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={handleNormalize}
                disabled={settingsControlsDisabled || pulseWeightSum === 0}
                className="h-6 px-2 text-xs"
                data-testid="normalize-button"
              >
                Normalize to 1.0
              </Button>
            </div>
          </div>

          {/* Discovery seed balance */}
          <div className="space-y-4 border-t pt-4">
            <div className="flex items-center justify-between">
              <h4 className="text-sm font-medium">Discovery seed balance</h4>
              <div className="flex items-center gap-2">
                <Label htmlFor="recommendation-enabled-toggle" className="text-xs">
                  Recommendations enabled
                </Label>
                <button
                  id="recommendation-enabled-toggle"
                  type="button"
                  role="switch"
                  aria-label="Recommendations enabled"
                  data-testid="recommendation-enabled-toggle"
                  aria-checked={!!recommendationEnabled}
                  onClick={() =>
                    setMut.mutate({ key: 'recommendation.enabled', value: !recommendationEnabled })
                  }
                  disabled={settingsControlsDisabled}
                  className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
                    recommendationEnabled ? 'bg-primary' : 'bg-input'
                  }`}
                >
                  <span
                    className={`pointer-events-none block h-5 w-5 rounded-full bg-background shadow-lg ring-0 transition-transform ${
                      recommendationEnabled ? 'translate-x-5' : 'translate-x-0'
                    }`}
                  />
                </button>
              </div>
            </div>

            <div className="space-y-1">
              <Label className="flex items-center justify-between text-xs">
                <span className="flex items-center gap-1">
                  Liked papers weight
                  <InfoTooltip content="How much to weight similarity to papers you've starred when seeding Pulse discovery." />
                </span>
                <span className="font-mono text-muted-foreground">
                  {Math.round(localLikedWeight * 100)}%
                </span>
              </Label>
              <input
                type="range"
                aria-label="Liked papers weight"
                min={0}
                max={1}
                step={0.05}
                value={localLikedWeight}
                onChange={(e) => setLocalLikedWeight(Number(e.target.value))}
                onPointerUp={() =>
                  setMut.mutate({ key: 'recommendation.liked_weight', value: localLikedWeight })
                }
                disabled={settingsControlsDisabled}
                className="w-full accent-primary"
              />
            </div>

            <div className="space-y-1">
              <Label className="flex items-center justify-between text-xs">
                <span className="flex items-center gap-1">
                  Project context weight
                  <InfoTooltip content="How much to weight relevance to your active projects when seeding Pulse discovery." />
                </span>
                <span className="font-mono text-muted-foreground">
                  {Math.round(localProjectWeight * 100)}%
                </span>
              </Label>
              <input
                type="range"
                aria-label="Project context weight"
                min={0}
                max={1}
                step={0.05}
                value={localProjectWeight}
                onChange={(e) => setLocalProjectWeight(Number(e.target.value))}
                onPointerUp={() =>
                  setMut.mutate({
                    key: 'recommendation.project_weight',
                    value: localProjectWeight,
                  })
                }
                disabled={settingsControlsDisabled}
                className="w-full accent-primary"
              />
            </div>
          </div>

          {/* L2 negative-feedback penalty */}
          <div className="space-y-3 border-t pt-4">
            <div>
              <h4 className="text-sm font-medium">L2 negative-feedback penalty</h4>
              <p className="text-xs text-muted-foreground">
                Strength of the cosine penalty applied to candidates similar to papers
                you&apos;ve thumbed-down. Default 0.5.
              </p>
            </div>
            <div className="flex items-center gap-4">
              <Slider
                min={0}
                max={2}
                step={0.05}
                value={[l2Lambda]}
                onValueChange={([v]) => setL2Lambda(v ?? l2Lambda)}
                onValueCommit={([v]) =>
                  setMut.mutate(
                    { key: 'pulse.l2_lambda', value: v },
                    {
                      onError: (err) =>
                        toast.error('Failed to update L2 lambda', {
                          description: errorMessage(err),
                        }),
                    },
                  )
                }
                disabled={settingsControlsDisabled}
                className="flex-1"
              />
              <span className="font-mono text-sm w-12 text-right">{l2Lambda.toFixed(2)}</span>
            </div>
          </div>
        </CardContent>
      )}
    </Card>
  );
}

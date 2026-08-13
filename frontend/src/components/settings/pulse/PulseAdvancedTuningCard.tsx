/**
 * PulseAdvancedTuningCard — collapsible card owning signal weights, discovery seed balance,
 * and L2 negative-feedback penalty.
 */
import { useState, useMemo, useRef } from 'react';
import { useQuery, type UseMutationResult } from '@tanstack/react-query';
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
import { ChevronDown, ChevronRight, Link as LinkIcon } from 'lucide-react';
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
import { getConfigValue } from './pulse-utils';
import { onSaveError } from '@/lib/forms/save-error';
import { fetchPulseDebug } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Link } from 'react-router-dom';
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
  /** Fires once the adjustment is over, not on every drag tick or key press. */
  onCommit: () => void;
  /** Optional status note rendered beside the label (e.g. classifier rating count). */
  statusNote?: string;
}

function WeightSliderRow({
  weightKey,
  value,
  disabled,
  capabilityPresent,
  onChange,
  onCommit,
  statusNote,
}: WeightSliderRowProps) {
  const gate = CONDITIONAL_SIGNAL_GATES[weightKey];

  // A keyboard user moves the slider one arrow key at a time, so saving on key
  // release would send a write per keystroke. Both input modes save once the
  // adjustment is over instead: on pointer release, or when focus leaves.
  const hasUnsavedChange = useRef(false);
  const saveIfChanged = () => {
    if (!hasUnsavedChange.current) return;
    hasUnsavedChange.current = false;
    onCommit();
  };

  const sliderInput = (
    <input
      type="range"
      aria-label={`${PULSE_WEIGHT_LABELS[weightKey]} weight`}
      data-testid={`weight-slider-${weightKey}`}
      min={0}
      max={1}
      step={0.05}
      value={value}
      onChange={(e) => {
        hasUnsavedChange.current = true;
        onChange(weightKey, Number(e.target.value));
      }}
      onPointerUp={saveIfChanged}
      onBlur={saveIfChanged}
      disabled={disabled || !capabilityPresent}
      className="w-full accent-primary disabled:opacity-40"
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
          {!capabilityPresent && gate && (
            <span
              data-testid={`inactive-chip-${weightKey}`}
              className="inline-flex items-center rounded px-1 py-0.5 text-[10px] font-medium bg-muted text-muted-foreground leading-none"
            >
              Inactive — missing dependency
            </span>
          )}
        </span>
        <span className="flex items-center gap-1.5 font-mono text-muted-foreground">
          {statusNote && (
            <span className="text-[10px] font-normal normal-case">{statusNote}</span>
          )}
          {value.toFixed(2)}
        </span>
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

  const { data: debugInfo } = useQuery({
    queryKey: QUERY_KEYS.pulse.debug(),
    queryFn: fetchPulseDebug,
    enabled: advancedOpen,
    staleTime: 30_000,
  });

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

  // These two save on pointer release, which a keyboard user never triggers —
  // their change would be shown and then silently dropped. Saving on blur as
  // well covers both input modes, and the flag keeps a pointer adjustment from
  // writing twice when focus then leaves.
  const weightDirty = useRef({ liked: false, project: false });
  const saveWeight = (which: 'liked' | 'project', key: string, value: number, label: string) => {
    if (!weightDirty.current[which]) return;
    weightDirty.current[which] = false;
    setMut.mutate({ key, value }, { onError: onSaveError(`Could not update the ${label}`) });
  };
  const [l2Lambda, setL2Lambda] = useSyncedState(l2LambdaConfig);
  const [localPulseWeights, setLocalPulseWeights] =
    useSyncedState<Record<PulseWeightKey, number>>(pulseWeightsServer);

  const onWeightsSaveError = onSaveError('Could not update the signal weights');

  const updatePulseWeight = (key: PulseWeightKey, value: number) => {
    if (settingsControlsDisabled) return;
    setLocalPulseWeights((prev) => ({ ...prev, [key]: value }));
  };

  const commitPulseWeights = () => {
    if (settingsControlsDisabled) return;
    setMut.mutate(
      { key: 'pulse.weights', value: localPulseWeights },
      { onError: onWeightsSaveError },
    );
  };

  const applyPreset = (preset: (typeof WEIGHT_PRESETS)[number]) => {
    if (settingsControlsDisabled) return;
    const next: Record<PulseWeightKey, number> = Object.assign(
      { ...DEFAULT_PULSE_WEIGHTS },
      preset.weights,
    );
    setLocalPulseWeights(next);
    setMut.mutate({ key: 'pulse.weights', value: next }, { onError: onWeightsSaveError });
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
    setMut.mutate({ key: 'pulse.weights', value: next }, { onError: onWeightsSaveError });
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
                  onCommit={commitPulseWeights}
                />
              ))}
            </div>

            {/* Optional signal sliders */}
            <div className="space-y-3 rounded-md border border-dashed p-3">
              <div>
                <h5 className="text-xs font-semibold">
                  Optional signals — need extra data or dependencies
                </h5>
                <p className="text-xs text-muted-foreground mt-0.5">
                  These signals are inactive by default. Enable them once the prerequisites are in
                  place.
                </p>
                <ul className="mt-1.5 space-y-0.5 text-[11px] text-muted-foreground">
                  <li>
                    <span className="font-medium">Citation signals</span> (Citation PageRank,
                    Citation count, Shared citation neighbourhood) — need the{' '}
                    <code className="rounded bg-muted px-0.5">networkx</code> package on the server.
                  </li>
                  <li>
                    <span className="font-medium">Personal classifier</span> — needs the{' '}
                    <code className="rounded bg-muted px-0.5">scikit-learn</code> package on the
                    server, plus ~30 Pulse ratings.
                  </li>
                </ul>
                <p className="mt-1.5 text-[11px] text-muted-foreground">
                  Need citation data?{' '}
                  <Link
                    to="/citations"
                    className="inline-flex items-center gap-0.5 text-primary underline-offset-2 hover:underline"
                  >
                    Open the Citation Graph
                    <LinkIcon className="h-2.5 w-2.5" />
                  </Link>
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
                  onCommit={commitPulseWeights}
                  statusNote={
                    key === 'classifier' && debugInfo?.classifier_sample_count != null
                      ? `${debugInfo.classifier_sample_count}/30 ratings`
                      : undefined
                  }
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
                    setMut.mutate(
                      { key: 'recommendation.enabled', value: !recommendationEnabled },
                      { onError: onSaveError('Could not update whether recommendations are used') },
                    )
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
                onChange={(e) => {
                  weightDirty.current.liked = true;
                  setLocalLikedWeight(Number(e.target.value));
                }}
                onPointerUp={() =>
                  saveWeight('liked', 'recommendation.liked_weight', localLikedWeight, 'liked papers weight')
                }
                onBlur={() =>
                  saveWeight('liked', 'recommendation.liked_weight', localLikedWeight, 'liked papers weight')
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
                onChange={(e) => {
                  weightDirty.current.project = true;
                  setLocalProjectWeight(Number(e.target.value));
                }}
                onPointerUp={() =>
                  saveWeight('project', 'recommendation.project_weight', localProjectWeight, 'project context weight')
                }
                onBlur={() =>
                  saveWeight('project', 'recommendation.project_weight', localProjectWeight, 'project context weight')
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
                aria-label="L2 negative-feedback penalty"
                min={0}
                max={2}
                step={0.05}
                value={[l2Lambda]}
                onValueChange={([v]) => setL2Lambda(v ?? l2Lambda)}
                onValueCommit={([v]) =>
                  setMut.mutate(
                    { key: 'pulse.l2_lambda', value: v },
                    { onError: onSaveError('Could not update the negative-feedback penalty') },
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

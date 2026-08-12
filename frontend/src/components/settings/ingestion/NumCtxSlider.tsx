import { useState, useMemo } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { setConfig } from '@/lib/api';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Slider } from '@/components/ui/slider';
import type { ConfigEntry, ModelFitDetail } from '@/types';
import { FitBadge, type FitStatus } from './FitBadge';
import {
  NUM_CTX_STOPS,
  isNumCtx,
  computeRequiredVram,
  fitStatus,
  largestFittingStop,
  clampToNonUnfit,
  hasFitBaseline,
  type HardwareInfoApi,
  type NumCtx,
} from './hardware-fit';

interface NumCtxSliderProps {
  role: 'smart' | 'fast' | 'embed';
  machineId: string;
  fitDetail?: ModelFitDetail;
  hardware?: HardwareInfoApi;
  /** Currently configured model id for this role (for thinking toggle) */
  modelId: string;
  supportsThinking: boolean;
  /** Config entries passed from parent (already fetched by IngestionSection). */
  configs: ConfigEntry[];
}

// Human-readable role name for the slider's accessible label (data-testid
// already keys interactions off the raw role; this keys the a11y name).
const ROLE_NAME: Record<NumCtxSliderProps['role'], string> = {
  smart: 'main model',
  fast: 'quick model',
  embed: 'embedding model',
};

export function NumCtxSlider({
  role,
  machineId,
  fitDetail,
  hardware,
  modelId,
  supportsThinking,
  configs,
}: NumCtxSliderProps) {
  const queryClient = useQueryClient();
  const vramGb = hardware?.vram_gb ?? 0;

  // Available stops — capped to max_num_ctx if known
  const availableStops = NUM_CTX_STOPS.filter(
    (s) => !fitDetail || s <= fitDetail.max_num_ctx,
  );
  const stops = availableStops.length > 0 ? availableStops : NUM_CTX_STOPS;

  const numCtxKey = `llm.${machineId}.${role}_num_ctx`;
  const thinkingKey = `llm.${machineId}.thinking_disabled.${modelId}`;

  const persistedNumCtx = configs.find((c) => c.key === numCtxKey)?.value;
  const persistedThinkingDisabled = configs.find((c) => c.key === thinkingKey)?.value;

  const defaultNumCtx = fitDetail?.default_num_ctx ?? 8192;
  const resolvedNumCtx =
    typeof persistedNumCtx === 'number'
      ? persistedNumCtx
      : typeof persistedNumCtx === 'string'
        ? parseInt(persistedNumCtx, 10) || defaultNumCtx
        : defaultNumCtx;

  // Snap to nearest stop
  const snapToStop = (val: number): number => {
    if (stops.length === 0) return 2048;
    let nearest = stops[0];
    for (const s of stops) {
      if (Math.abs(s - val) < Math.abs(nearest - val)) nearest = s;
    }
    return nearest;
  };

  const initialStop = snapToStop(resolvedNumCtx);

  // Local slider state (index into stops array)
  const [sliderIndex, setSliderIndex] = useState<number>(() => {
    const idx = isNumCtx(initialStop) ? stops.indexOf(initialStop) : -1;
    return idx >= 0 ? idx : 0;
  });

  // Inline save-failure message (a silent slider snap would look saved)
  const [saveError, setSaveError] = useState<string | null>(null);

  const currentStop = stops[sliderIndex] ?? stops[0];

  // Memoize required VRAM for the current slider position (used in fit badge + label rows)
  const req = useMemo(
    () => (fitDetail ? computeRequiredVram(fitDetail, currentStop) : null),
    [fitDetail, currentStop],
  );

  // Compute live fit for current slider position
  const computedFit = (() => {
    if (!fitDetail) return null;
    if (vramGb <= 0) return 'unknown' as const;
    if (req === null) return null;
    return fitStatus(req, vramGb);
  })();

  const largestFitting = fitDetail && vramGb > 0
    ? largestFittingStop(fitDetail, vramGb, stops)
    : undefined;

  // Determine fit badge status: use computed if we have data, else fall back to fit_detail.default
  const badgeStatus = computedFit ?? fitDetail?.default ?? 'unknown';

  // Thinking-mode persisted value: default true for supports_thinking models
  const thinkingDisabled =
    typeof persistedThinkingDisabled === 'boolean'
      ? persistedThinkingDisabled
      : typeof persistedThinkingDisabled === 'string'
        ? persistedThinkingDisabled === 'true'
        : supportsThinking; // default: disabled=true for thinking-capable

  const saveMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      setSaveError(null);
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
    },
    onError: (error: Error, variables) => {
      setSaveError(`Failed to save: ${error.message}`);
      if (variables.key === numCtxKey) {
        // Roll the slider back to the persisted value — a failed save must
        // not keep looking saved.
        const persistedIdx = isNumCtx(initialStop) ? stops.indexOf(initialStop) : -1;
        setSliderIndex(persistedIdx >= 0 ? persistedIdx : 0);
      }
    },
  });

  const handleSliderChange = (values: number[]) => {
    const idx = values[0] ?? 0;
    const rawStop = stops[idx] ?? stops[0];

    // Clamp: block unfit stops
    let clampedStop: NumCtx = rawStop;
    if (fitDetail && hasFitBaseline(fitDetail) && vramGb > 0) {
      const clamped = clampToNonUnfit(rawStop, fitDetail, vramGb, stops);
      clampedStop = isNumCtx(clamped) ? clamped : rawStop;
    }
    const clampedIdx = stops.indexOf(clampedStop);
    setSliderIndex(clampedIdx >= 0 ? clampedIdx : 0);
  };

  const handleSliderCommit = (values: number[]) => {
    const idx = values[0] ?? 0;
    const rawStop = stops[idx] ?? stops[0];
    let clampedStop: NumCtx = rawStop;
    if (fitDetail && hasFitBaseline(fitDetail) && vramGb > 0) {
      const clamped = clampToNonUnfit(rawStop, fitDetail, vramGb, stops);
      clampedStop = isNumCtx(clamped) ? clamped : rawStop;
    }
    saveMut.mutate({ key: numCtxKey, value: clampedStop });
  };

  return (
    <div className="mt-3 space-y-3 border-t pt-3" data-testid={`num-ctx-slider-${role}`}>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label className="text-xs text-muted-foreground">
            Reading window — how much of each paper the AI reads at once (tokens): <span className="font-medium text-foreground">{currentStop.toLocaleString()}</span>
          </Label>
          {badgeStatus !== 'unknown' && (
            <FitBadge
              status={badgeStatus as FitStatus}
              requiredVramGb={req}
              availableVramGb={vramGb > 0 ? vramGb : undefined}
              largestFitting={largestFitting}
            />
          )}
        </div>
        {/* GB / GB detail — kept off the badge pill, surfaced here behind the expander. */}
        {req != null && vramGb > 0 && (
          <p className="text-xs text-muted-foreground" data-testid={`fit-detail-${role}`}>
            {req.toFixed(1)} GB / {vramGb.toFixed(1)} GB VRAM
          </p>
        )}
        <Slider
          aria-label={`Reading window for the ${ROLE_NAME[role]}`}
          min={0}
          max={stops.length - 1}
          step={1}
          value={[sliderIndex]}
          onValueChange={handleSliderChange}
          onValueCommit={handleSliderCommit}
          data-testid={`slider-${role}`}
        />
        <div className="flex justify-between text-xs text-muted-foreground">
          {stops.map((s) => {
            const stopReq = fitDetail ? computeRequiredVram(fitDetail, s) : null;
            const isUnfit = stopReq !== null && vramGb > 0
              ? fitStatus(stopReq, vramGb) === 'unfit'
              : false;
            return (
              <span
                key={s}
                className={isUnfit ? 'text-red-400 line-through' : ''}
                title={isUnfit ? 'Unfit at this context length' : undefined}
              >
                {s >= 1024 ? `${s / 1024}k` : String(s)}
              </span>
            );
          })}
        </div>
        {saveError && (
          <p
            className="text-sm text-destructive"
            role="alert"
            data-testid={`num-ctx-save-error-${role}`}
          >
            {saveError}
          </p>
        )}
      </div>

      {supportsThinking && (
        <div className="flex items-center gap-2 pt-1" data-testid={`thinking-toggle-${role}`}>
          <Switch
            id={`thinking-disabled-${role}`}
            checked={thinkingDisabled}
            onCheckedChange={(checked) => saveMut.mutate({ key: thinkingKey, value: checked })}
            disabled={saveMut.isPending}
          />
          <Label htmlFor={`thinking-disabled-${role}`} className="text-xs cursor-pointer">
            Disable thinking mode{' '}
            <span className="text-muted-foreground">(recommended for Qwen3)</span>
          </Label>
        </div>
      )}
    </div>
  );
}

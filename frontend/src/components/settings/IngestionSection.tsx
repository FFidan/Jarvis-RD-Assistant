import { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchConfig, setConfig, apiFetch } from '@/lib/api';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Slider } from '@/components/ui/slider';
import { Card, CardContent } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { Settings2, ChevronDown, ChevronRight } from 'lucide-react';
import { ModelSelector } from '@/components/shared/ModelSelector';
import type { ConfigEntry, ModelFitDetail } from '@/types';
import { ConfigEntryCard } from './ingestion/ConfigEntryCard';

// ---------------------------------------------------------------------------
// Hardware-Aware Settings helpers (Contract 06)
// ---------------------------------------------------------------------------

/** Power-of-2 snap steps for the num_ctx slider (§10.2). */
const NUM_CTX_STOPS = [2048, 4096, 8192, 16384, 32768, 65536] as const;
type NumCtx = (typeof NUM_CTX_STOPS)[number];
const isNumCtx = (n: number): n is NumCtx => (NUM_CTX_STOPS as readonly number[]).includes(n);

interface HardwareInfoApi {
  vram_gb?: number;
  vram_source?: string;
  tier?: number;
  detected_at?: string;
  machine_id?: string;
}

interface ModelCatalogEntryApi {
  id: string;
  name: string;
  provider: string;
  roles: string[];
  fit_detail?: ModelFitDetail;
  supports_thinking?: boolean;
}

/** Per-alias recommendation entry returned by GET /api/system/models hardware_recommendation. */
interface HardwareRecommendationAlias {
  alias: 'smart' | 'fast' | 'embed';
  model: string;
  confirm_on_target: boolean;
  notes: string;
}

/**
 * Hardware-fit advisory returned by GET /api/system/models (B3-1 contract).
 * Optional — older backends omit this field; UI degrades gracefully.
 */
interface HardwareRecommendation {
  vram_mb: number | null;
  bucket: 'CPU_ONLY' | 'ENTRY' | 'MID' | 'MID_HIGH' | 'HIGH';
  summary: string;
  aliases: HardwareRecommendationAlias[];
}

interface SystemModelsApi {
  hardware?: HardwareInfoApi;
  catalog?: ModelCatalogEntryApi[];
  /** Advisory per-VRAM model recommendation. Optional — absent on older backends. */
  hardware_recommendation?: HardwareRecommendation;
}

type FitDetailWithBaseline = ModelFitDetail & {
  base_vram_gb?: number | null;
  base_num_ctx?: number | null;
};

function hasFitBaseline(
  fitDetail: ModelFitDetail,
): fitDetail is ModelFitDetail & { base_vram_gb: number; base_num_ctx: number } {
  const detail: FitDetailWithBaseline = fitDetail;
  return (
    typeof detail.base_vram_gb === 'number' &&
    Number.isFinite(detail.base_vram_gb) &&
    typeof detail.base_num_ctx === 'number' &&
    Number.isFinite(detail.base_num_ctx)
  );
}

/**
 * Compute required VRAM for a model at a given num_ctx.
 * Uses the backend-provided baseline fields; older fit_detail payloads return null.
 */
function computeRequiredVram(
  fitDetail: ModelFitDetail,
  numCtx: number,
): number | null {
  if (!hasFitBaseline(fitDetail)) return null;
  const kvBytes = fitDetail.kv_cache_bytes_per_token ?? 1024;
  const extraTokens = Math.max(0, numCtx - fitDetail.base_num_ctx);
  return fitDetail.base_vram_gb + (extraTokens * kvBytes) / 1e9;
}

/**
 * Determine fit status for a given required VRAM vs available VRAM.
 * Contract 06 §4 thresholds.
 */
function fitStatus(
  requiredVramGb: number,
  availableVramGb: number,
): 'fits' | 'partial' | 'unfit' {
  if (requiredVramGb <= availableVramGb * 0.85) return 'fits';
  if (requiredVramGb <= availableVramGb * 1.2) return 'partial';
  return 'unfit';
}

/**
 * Find the highest snap-step that produces 'fits' (≤ 85% VRAM threshold).
 * Falls back to the lowest stop if nothing fits.
 */
function largestFittingStop(
  fitDetail: ModelFitDetail,
  vramGb: number,
  stops: readonly number[],
): number {
  let best: number = stops[0] ?? 2048;
  for (const stop of stops) {
    if (stop > fitDetail.max_num_ctx) break;
    const req = computeRequiredVram(fitDetail, stop);
    if (req !== null && req <= vramGb * 0.85) best = stop;
  }
  return best;
}

/**
 * Clamp a slider value to the highest non-unfit stop (fits or partial).
 * Partial (up to 120%) is allowed; only unfit is blocked (§10.3).
 */
function clampToNonUnfit(
  value: number,
  fitDetail: ModelFitDetail,
  vramGb: number,
  stops: readonly number[],
): number {
  const allowed = stops.filter((s) => {
    const req = computeRequiredVram(fitDetail, s);
    return s <= fitDetail.max_num_ctx && req !== null && fitStatus(req, vramGb) !== 'unfit';
  });
  if (allowed.length === 0) return stops[0] ?? 2048;
  // If current value is allowed, keep it; otherwise clamp to max allowed
  if (allowed.includes(value)) return value;
  const sorted = [...allowed].sort((a, b) => a - b);
  return sorted[sorted.length - 1] ?? 2048;
}

// ---------------------------------------------------------------------------
// HardwareStrip — §6.2
// ---------------------------------------------------------------------------

interface HardwareStripProps {
  hardware: HardwareInfoApi;
}

function HardwareStrip({ hardware }: HardwareStripProps) {
  const [expanded, setExpanded] = useState(false);
  if (!hardware.vram_gb && hardware.tier === undefined) return null;

  const summary = [
    typeof hardware.vram_gb === 'number' ? `${hardware.vram_gb.toFixed(1)} GB VRAM` : null,
    typeof hardware.tier === 'number' ? `Tier ${hardware.tier}` : null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <button
      type="button"
      aria-expanded={expanded}
      className="mb-3 w-full cursor-pointer rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground select-none text-left"
      onClick={() => setExpanded((v) => !v)}
      data-testid="hardware-strip"
    >
      <span className="font-medium text-foreground">{summary}</span>
      {expanded && (
        <span className="ml-3 space-x-3">
          {hardware.vram_source && (
            <span>
              Source: <span className="text-foreground">{hardware.vram_source}</span>
            </span>
          )}
          {hardware.detected_at && (
            <span>
              Detected:{' '}
              <span className="text-foreground">
                {new Date(hardware.detected_at).toLocaleString()}
              </span>
            </span>
          )}
        </span>
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// HardwareRecommendationBanner — B3-2 (per-VRAM advisory)
// ---------------------------------------------------------------------------

interface HardwareRecommendationBannerProps {
  recommendation: HardwareRecommendation;
}

/**
 * Advisory banner surfacing the backend's per-VRAM model recommendation.
 * This is informational only — the operator still picks via the model picker.
 * Renders the summary line + per-alias recommended-model rows.
 * Handles the vram_mb:null / aliases:[] case gracefully (GPU probe failed).
 */
function HardwareRecommendationBanner({ recommendation }: HardwareRecommendationBannerProps) {
  const { summary, aliases } = recommendation;
  const hasAliases = aliases.length > 0;

  return (
    <div
      className="mb-3 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs dark:border-blue-800 dark:bg-blue-950"
      data-testid="hw-recommendation-banner"
    >
      <p className="font-medium text-blue-900 dark:text-blue-100">{summary}</p>
      <p className="mt-0.5 text-blue-700 dark:text-blue-300">
        Advisory — does not change your active model automatically.
      </p>
      {hasAliases && (
        <ul className="mt-2 space-y-1" data-testid="hw-recommendation-alias-list">
          {aliases.map((entry) => (
            <li key={entry.alias} className="flex flex-wrap items-center gap-x-2 text-blue-800 dark:text-blue-200">
              <span className="font-medium">{entry.alias}</span>
              <span className="text-muted-foreground">→</span>
              <span className="font-mono">{entry.model}</span>
              {entry.confirm_on_target && (
                <span
                  className="inline-flex items-center rounded-full bg-amber-100 px-1.5 py-0.5 text-amber-800 dark:bg-amber-900 dark:text-amber-200"
                  data-testid={`confirm-on-target-${entry.alias}`}
                  role="note"
                  aria-label="Confirm on target hardware before switching"
                >
                  confirm on target
                </span>
              )}
              {entry.notes && (
                <span className="text-blue-600 dark:text-blue-400">{entry.notes}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FitBadge — §6.1
// ---------------------------------------------------------------------------

interface FitBadgeProps {
  status: 'fits' | 'partial' | 'unfit' | 'cloud' | 'unknown';
  requiredVramGb?: number | null;
  availableVramGb?: number;
  largestFitting?: number;
}

function FitBadge({ status, requiredVramGb, availableVramGb, largestFitting }: FitBadgeProps) {
  const fmt = (n: number) => n.toFixed(1);
  const colorClass =
    status === 'fits'
      ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
      : status === 'partial'
        ? 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200'
        : status === 'unfit'
          ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'
          : 'bg-muted text-muted-foreground';

  let copy: string;
  switch (status) {
    case 'fits':
      copy =
        requiredVramGb !== null && requiredVramGb !== undefined && availableVramGb !== undefined
          ? `Fits — ${fmt(requiredVramGb)} GB / ${fmt(availableVramGb)} GB`
          : 'Fits';
      break;
    case 'partial':
      copy =
        requiredVramGb !== null && requiredVramGb !== undefined && availableVramGb !== undefined
          ? `Partial offload — ${fmt(requiredVramGb)} GB / ${fmt(availableVramGb)} GB · slower`
          : 'Partial offload · slower';
      break;
    case 'unfit':
      copy =
        largestFitting !== undefined
          ? `Won't fit · try ${largestFitting.toLocaleString()} tokens`
          : "Won't fit";
      break;
    case 'cloud':
      copy = 'Cloud';
      break;
    default:
      copy = 'Unknown VRAM';
  }

  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${colorClass}`}
      data-testid={`fit-badge-${status}`}
    >
      {copy}
    </span>
  );
}

// ---------------------------------------------------------------------------
// NumCtxSlider — per-role expander
// ---------------------------------------------------------------------------

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

function NumCtxSlider({
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
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() }),
  });

  const handleSliderChange = (values: number[]) => {
    const idx = values[0] ?? 0;
    const rawStop = stops[idx] ?? stops[0];

    // Clamp: block unfit stops (§10.3)
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
            Context length (num_ctx): <span className="font-medium text-foreground">{currentStop.toLocaleString()}</span>
          </Label>
          {badgeStatus !== 'unknown' && (
            <FitBadge
              status={badgeStatus as 'fits' | 'partial' | 'unfit' | 'cloud' | 'unknown'}
              requiredVramGb={req}
              availableVramGb={vramGb > 0 ? vramGb : undefined}
              largestFitting={largestFitting}
            />
          )}
        </div>
        <Slider
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

// ---------------------------------------------------------------------------
// LlmModelCard — wraps ModelSelector + Configure expander
// ---------------------------------------------------------------------------

interface LlmModelCardProps {
  entry: ConfigEntry;
  meta: { label: string; description: string };
  machineId: string;
  hardware?: HardwareInfoApi;
  catalogEntry?: ModelCatalogEntryApi;
  onSave: (key: string, value: unknown) => void;
  isPending: boolean;
  /** Config entries from parent (to avoid a second fetch in NumCtxSlider). */
  configs: ConfigEntry[];
}

function LlmModelCard({
  entry,
  meta,
  machineId,
  hardware,
  catalogEntry,
  onSave,
  isPending,
  configs,
}: LlmModelCardProps) {
  const [configureOpen, setConfigureOpen] = useState(false);

  const rawValue = typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value);
  const currentValue = rawValue.replace(/^"|"$/g, '');

  // Determine the role from config key
  const role = entry.key.replace(/^llm\./, '').replace(/_model$/, '') as 'smart' | 'fast' | 'embed';
  const isValidRole = role === 'smart' || role === 'fast' || role === 'embed';

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardContent className="flex items-center gap-4 p-4">
        <div className="flex-1 min-w-0 space-y-2">
          <div className="font-medium text-sm">{meta.label}</div>
          <p className="text-xs text-muted-foreground">{meta.description}</p>
          <ModelSelector
            value={currentValue}
            onChange={(v) => onSave(entry.key, v)}
            configKey={entry.key}
          />

          {isValidRole && (
            <div>
              <button
                type="button"
                className="mt-1 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                onClick={() => setConfigureOpen((v) => !v)}
                data-testid={`configure-toggle-${role}`}
                disabled={isPending}
              >
                {configureOpen ? (
                  <ChevronDown className="h-3 w-3" />
                ) : (
                  <ChevronRight className="h-3 w-3" />
                )}
                Configure
              </button>

              {configureOpen && (
                <NumCtxSlider
                  role={role}
                  machineId={machineId}
                  fitDetail={catalogEntry?.fit_detail}
                  hardware={hardware}
                  modelId={currentValue}
                  supportsThinking={catalogEntry?.supports_thinking ?? false}
                  configs={configs}
                />
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Config metadata for human-readable labels and grouping
// ---------------------------------------------------------------------------

/** Keys that belong to other tabs (Pulse, Setup, Telegram, Automation) and
 *  should not appear in the "Models & Preferences" ingestion section. */
const HIDE_FROM_UI = new Set([
  'setup.completed',
  'telegram.owner_chat_id',
  'pulse.cron',
  'pulse.enabled',
  'pulse.deck_size',
  'pulse.stage2_top_k',
  'pulse.weights',
  // Owned exclusively by AutomationSection (timezone combobox)
  'user.timezone',
]);

const CONFIG_METADATA: Record<
  string,
  {
    label: string;
    description: string;
    group: string;
    tooltip?: string;
    type?: 'boolean' | 'number' | 'string';
    min?: number;
    max?: number;
    step?: number;
  }
> = {
  'fsrs.desired_retention': {
    label: 'Target Retention',
    description: 'Desired probability of recalling a card correctly (0.0\u20131.0)',
    group: 'Spaced Repetition',
    tooltip:
      'Desired probability of recalling a card correctly at review time. 0.9 = 90% recall. Higher values = more frequent review sessions.',
    type: 'number',
    min: 0.7,
    max: 1.0,
    step: 0.01,
  },
  'fsrs.learning_steps': {
    label: 'Learning Steps',
    description: 'Steps before a card graduates, as [min, max] minutes',
    group: 'Spaced Repetition',
    tooltip:
      "Minutes between a new card's first review attempts before it enters the FSRS long-term schedule. [1, 10] = reviewed after 1 min, then 10 min.",
  },
  'llm.embed_model': {
    label: 'Embedding Model',
    description: 'Model used for generating text embeddings',
    group: 'LLM Models',
  },
  'llm.fast_model': {
    label: 'Fast Model',
    description: 'Lightweight model for quick tasks like tagging and decomposition',
    group: 'LLM Models',
  },
  'llm.smart_model': {
    label: 'Smart Model',
    description: 'High-capability model for summarization, extraction, and RAG',
    group: 'LLM Models',
  },
};
// Note: 'user.timezone' is intentionally excluded from CONFIG_METADATA here;
// it is owned exclusively by AutomationSection (searchable combobox).

/** Preferred order for groups (unlisted groups sort alphabetically after these).
 *  Keys without metadata fall into 'Other' which is intentionally omitted here
 *  so they disappear rather than exposing raw JSON to the UI. */
const GROUP_ORDER = ['LLM Models', 'Spaced Repetition', 'Preferences'];

// ---------------------------------------------------------------------------
// IngestionSection
// ---------------------------------------------------------------------------

interface IngestionSectionProps {
  /**
   * Optional allow-list of group labels to render (must match the exact
   * strings in {@link GROUP_ORDER}). When omitted the full set of groups is
   * rendered (default, backward-compatible behavior). When provided, only the
   * listed groups are shown — used by SpacedRepetitionSection to scope §VI
   * Research → Spaced Repetition to the `fsrs.*` group alone (Conflict-5).
   */
  filterGroups?: string[];
}

export function IngestionSection({ filterGroups }: IngestionSectionProps = {}) {
  const queryClient = useQueryClient();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [saveError, setSaveError] = useState<string | null>(null);

  const { data: configs = [], isLoading } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });

  // Fetch system models to get hardware info + catalog fit_detail
  const { data: systemModels } = useQuery<SystemModelsApi>({
    queryKey: QUERY_KEYS.config.systemModels(),
    queryFn: () => apiFetch<SystemModelsApi>('/api/system/models'),
    staleTime: 60_000,
  });

  const hardware = systemModels?.hardware;
  const catalog = systemModels?.catalog ?? [];
  // machine_id from hardware response (Contract 06 §3)
  const machineId = hardware?.machine_id ?? 'local';
  // B3-2: per-VRAM advisory recommendation (optional — absent on older backends)
  const hardwareRecommendation = systemModels?.hardware_recommendation;

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() });
      setEditingKey(null);
      setSaveError(null);
    },
    onError: (error: Error) => {
      setSaveError(`Failed to save: ${error.message}`);
    },
  });

  const startEdit = (entry: ConfigEntry) => {
    setEditingKey(entry.key);
    setEditValue(typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value));
  };

  const saveEdit = () => {
    if (!editingKey) return;
    let parsed: unknown = editValue;
    try {
      parsed = JSON.parse(editValue);
    } catch {
      // keep as string
    }
    setMut.mutate({ key: editingKey, value: parsed });
  };

  if (isLoading) {
    return <div className="py-8 text-center text-muted-foreground">Loading config...</div>;
  }

  if (configs.length === 0) {
    return (
      <EmptyState
        title="No config entries"
        description="Ingestion config will appear here once set."
        icon={Settings2}
      />
    );
  }

  // Filter out keys owned by other tabs
  const visibleEntries = configs.filter((e) => !HIDE_FROM_UI.has(e.key));

  // Group configs by metadata group; skip entries without known metadata
  // ('Other' group is intentionally not rendered — unknown keys silently disappear)
  const grouped = visibleEntries.reduce<Record<string, ConfigEntry[]>>((acc, entry) => {
    const group = CONFIG_METADATA[entry.key]?.group;
    if (!group) return acc; // unknown key — don't expose raw JSON
    (acc[group] ??= []).push(entry);
    return acc;
  }, {});

  // Sort groups by preferred order, then optionally restrict to the
  // caller-provided allow-list (default: all groups — backward compatible).
  const sortedGroups = Object.keys(grouped)
    .sort((a, b) => {
      const ia = GROUP_ORDER.indexOf(a);
      const ib = GROUP_ORDER.indexOf(b);
      const oa = ia === -1 ? GROUP_ORDER.length : ia;
      const ob = ib === -1 ? GROUP_ORDER.length : ib;
      return oa - ob || a.localeCompare(b);
    })
    .filter((g) => filterGroups === undefined || filterGroups.includes(g));

  /**
   * Find the catalog entry matching the currently configured model for a given role.
   * Config value may be a bare model id (e.g. "qwen3:14b").
   */
  const findCatalogEntry = (configValue: unknown, role: string): ModelCatalogEntryApi | undefined => {
    const val = typeof configValue === 'string' ? configValue.replace(/^"|"$/g, '') : '';
    if (!val) return undefined;
    return catalog.find(
      (c) => c.roles.includes(role) && (c.id === val || c.id.replace(/:latest$/, '') === val),
    );
  };

  const llmGroup = grouped['LLM Models'];

  return (
    <div className="space-y-2">
      {sortedGroups.map((group) => (
        <div key={group}>
          <h4 className="mt-4 mb-2 text-sm font-semibold text-muted-foreground first:mt-0">
            {group}
          </h4>
          {/* Hardware strip — shown once at top of LLM Models group (§6.2) */}
          {group === 'LLM Models' && hardware && (
            <HardwareStrip hardware={hardware} />
          )}
          {/* B3-2: Per-VRAM advisory recommendation banner */}
          {group === 'LLM Models' && hardwareRecommendation && (
            <HardwareRecommendationBanner recommendation={hardwareRecommendation} />
          )}
          <div className="space-y-2">
            {(grouped[group] ?? []).map((entry) => {
              const meta = CONFIG_METADATA[entry.key];
              const isLlm = entry.key.startsWith('llm.');
              const customElement = isLlm ? (() => {
                const role = entry.key.replace(/^llm\./, '').replace(/_model$/, '');
                const catalogEntry = findCatalogEntry(entry.value, role);
                return (
                  <LlmModelCard
                    key={entry.key}
                    entry={entry}
                    meta={{ label: meta?.label ?? entry.key, description: meta?.description ?? '' }}
                    machineId={machineId}
                    hardware={hardware}
                    catalogEntry={catalogEntry}
                    onSave={(key, value) => setMut.mutate({ key, value })}
                    isPending={setMut.isPending}
                    configs={configs}
                  />
                );
              })() : undefined;
              return (
                <ConfigEntryCard
                  key={entry.key}
                  entry={entry}
                  meta={meta}
                  customElement={customElement}
                  editingKey={editingKey}
                  editValue={editValue}
                  saveError={saveError}
                  isMutPending={setMut.isPending}
                  onMutate={(key, value) => setMut.mutate({ key, value })}
                  onStartEdit={startEdit}
                  onEditValueChange={setEditValue}
                  onSaveEdit={saveEdit}
                  onCancelEdit={() => setEditingKey(null)}
                />
              );
            })}
          </div>
        </div>
      ))}
      {/* Render hardware strip + recommendation even if LLM Models group is absent (edge case) */}
      {!llmGroup && hardware && (
        <HardwareStrip hardware={hardware} />
      )}
      {!llmGroup && hardwareRecommendation && (
        <HardwareRecommendationBanner recommendation={hardwareRecommendation} />
      )}
    </div>
  );
}

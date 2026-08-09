import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchConfig, setConfig, fetchSystemModels } from '@/lib/api';
import type { HardwareRecommendation } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { Settings2, ChevronDown, ChevronRight } from 'lucide-react';
import { ModelSelector } from '@/components/shared/ModelSelector';
import type { ConfigEntry } from '@/types';
import { ConfigEntryCard } from './ingestion/ConfigEntryCard';
import { HardwareStrip } from './ingestion/HardwareStrip';
import { NumCtxSlider } from './ingestion/NumCtxSlider';
import {
  type HardwareInfoApi,
  type ModelCatalogEntryApi,
} from './ingestion/hardware-fit';

// ---------------------------------------------------------------------------
// FirstBootModelBanner — shown once after autoconfigure picks a model
// ---------------------------------------------------------------------------

interface FirstBootModelBannerProps {
  /** The CURRENT smart model autoconfigure actually seeded on first boot. */
  smartModel?: string;
  vramGb?: number;
}

/**
 * Shown after setup autoconfigure has seeded the smart model.
 * Renders "We picked {model} for your {vram} GB GPU — change anytime in Settings → Models."
 *
 * Feeds off the CURRENT smart model (what autoconfigure actually wrote), NOT the
 * static hardware-recommendation bucket — on boxes where the bucket recommends a
 * model that was never pulled (e.g. 48 GB → qwen3:30b-a3b while only qwen3:8b is
 * installed) the recommendation would assert a pick that never happened. Hidden
 * when there is no current smart model or no GPU (vram null / 0).
 */
function FirstBootModelBanner({ smartModel, vramGb }: FirstBootModelBannerProps) {
  if (!smartModel || vramGb === undefined || vramGb <= 0) return null;

  return (
    <div
      className="mb-2 rounded-md border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-900 dark:border-green-800 dark:bg-green-950 dark:text-green-100"
      data-testid="first-boot-model-banner"
    >
      We picked <span className="font-mono font-medium">{smartModel}</span> for your{' '}
      {vramGb.toFixed(1)} GB GPU — change anytime in Settings → Models
    </div>
  );
}

// ---------------------------------------------------------------------------
// HardwareRecommendationBanner — per-VRAM advisory
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
  /** LiteLLM delivery state for this role — "pending_restart" shows the pill. */
  deliveryStatus?: 'pending_restart' | 'applied';
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
  deliveryStatus,
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
          <div className="flex items-center gap-2">
            <div className="font-medium text-sm">{meta.label}</div>
            {deliveryStatus === 'pending_restart' && (
              <span
                className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900 dark:text-amber-200"
                data-testid={`delivery-pending-${role}`}
                title="Saved. The model service is temporarily unavailable, so JARVIS retries automatically and applies your choice as soon as it recovers. Answers keep using the previous model until then."
              >
                pending — applying automatically
              </span>
            )}
          </div>
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
    label: 'Embedding model (embed)',
    description:
      'Powers search across your library. Fixed once chosen — switching it requires re-indexing every paper.',
    group: 'AI models',
  },
  'llm.fast_model': {
    label: 'Quick model (fast)',
    description:
      'Scores and triages incoming papers. Pick a small, fast model — your choice applies automatically.',
    group: 'AI models',
  },
  'llm.smart_model': {
    label: 'Main model (smart)',
    description:
      'Writes your summaries, cards, and Ask answers. Pick the strongest model your GPU fits — your choice applies automatically.',
    group: 'AI models',
  },
};
// Note: 'user.timezone' is intentionally excluded from CONFIG_METADATA here;
// it is owned exclusively by AutomationSection (searchable combobox).

/** Preferred order for groups (unlisted groups sort alphabetically after these).
 *  Keys without metadata fall into 'Other' which is intentionally omitted here
 *  so they disappear rather than exposing raw JSON to the UI. */
const GROUP_ORDER = ['AI models', 'Spaced Repetition', 'Preferences'];

// ---------------------------------------------------------------------------
// IngestionSection
// ---------------------------------------------------------------------------

interface IngestionSectionProps {
  /**
   * Optional allow-list of group labels to render (must match the exact
   * strings in {@link GROUP_ORDER}). When omitted the full set of groups is
   * rendered (default, backward-compatible behavior). When provided, only the
   * listed groups are shown — used by SpacedRepetitionSection to scope
   * Research → Spaced Repetition to the `fsrs.*` group alone (Conflict-5).
   */
  filterGroups?: string[];
}

export function IngestionSection({ filterGroups }: IngestionSectionProps = {}) {
  const queryClient = useQueryClient();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  // Keyed by config key so a failed save paints ONLY under the card that
  // failed — a single section-global string would render under every card
  // taking the custom-element path (all three model cards).
  const [saveError, setSaveError] = useState<{ key: string; message: string } | null>(null);

  const { data: configs = [], isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });

  // Fetch system models to get hardware info + catalog fit_detail
  const { data: systemModels } = useQuery({
    queryKey: QUERY_KEYS.config.systemModels(),
    queryFn: ({ signal }) => fetchSystemModels(signal),
    staleTime: 60_000,
  });

  const hardware = systemModels?.hardware;
  const catalog = systemModels?.catalog ?? [];
  // machine_id from hardware response
  const machineId = hardware?.machine_id ?? 'local';
  // per-VRAM advisory recommendation (optional — absent on older backends)
  const hardwareRecommendation = systemModels?.hardware_recommendation;
  // First-boot banner shows the CURRENT smart model autoconfigure actually
  // seeded (current.smart_model), not the static bucket recommendation.
  const currentSmartModel = systemModels?.current?.smart_model;
  // The concise "we picked X" banner only renders for a seeded model on a GPU
  // (mirrors FirstBootModelBanner's own guard). When it can't render, the
  // per-VRAM recommendation is the single advisory instead — never both.
  const showFirstBootBanner = Boolean(currentSmartModel) && (hardware?.vram_gb ?? 0) > 0;

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() });
      setEditingKey(null);
      setSaveError(null);
    },
    onError: (error: Error, variables) => {
      setSaveError({ key: variables.key, message: `Failed to save: ${error.message}` });
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

  if (isError) {
    return (
      <p className="py-8 text-center text-sm text-destructive" role="alert" data-testid="config-load-error">
        Failed to load configuration. Check service health and try again.
      </p>
    );
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

  const llmGroup = grouped['AI models'];

  return (
    <div className="space-y-2">
      {sortedGroups.map((group) => (
        <div key={group}>
          <h4 className="mt-4 mb-2 text-sm font-semibold text-muted-foreground first:mt-0">
            {group}
          </h4>
          {group === 'AI models' && (
            <p className="mb-3 text-sm text-muted-foreground" data-testid="llm-models-description">
              Choose the models that read and write your research. We pick sensible
              defaults for your GPU and apply changes for you.
            </p>
          )}
          {/* Hardware strip — shown once at top of AI models group */}
          {group === 'AI models' && hardware && (
            <HardwareStrip hardware={hardware} />
          )}
          {/* ONE advisory only — the concise "we picked X" line when a model is
              already seeded on a GPU, otherwise the per-VRAM recommendation. */}
          {group === 'AI models' && showFirstBootBanner ? (
            <FirstBootModelBanner smartModel={currentSmartModel} vramGb={hardware?.vram_gb} />
          ) : (
            group === 'AI models' && hardwareRecommendation && (
              <HardwareRecommendationBanner recommendation={hardwareRecommendation} />
            )
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
                    deliveryStatus={systemModels?.delivery?.[role]}
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
                  saveError={saveError?.key === entry.key ? saveError.message : null}
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
      {/* Render hardware strip + recommendation even if AI models group is absent (edge case) */}
      {!llmGroup && hardware && (
        <HardwareStrip hardware={hardware} />
      )}
      {!llmGroup && showFirstBootBanner ? (
        <FirstBootModelBanner smartModel={currentSmartModel} vramGb={hardware?.vram_gb} />
      ) : (
        !llmGroup && hardwareRecommendation && (
          <HardwareRecommendationBanner recommendation={hardwareRecommendation} />
        )
      )}
    </div>
  );
}

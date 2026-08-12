import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { ModelSelector } from '@/components/shared/ModelSelector';
import {
  isLocalModel,
  matchesModelId,
  modelPriceLabel,
} from '@/components/shared/model-picker/model-options';
import { docsUrl } from '@/lib/docs-links';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchConfig, fetchSystemModels, listProviders, setConfig } from '@/lib/api';
import type {
  HardwareRecommendation,
  ModelCatalogEntry,
  ProviderMetadata,
  ProviderModelListStatus,
} from '@/lib/api';
import type { ConfigEntry } from '@/types';
import { ConfigEntryCard } from './ingestion/ConfigEntryCard';
import { HardwareStrip } from './ingestion/HardwareStrip';
import { NumCtxSlider } from './ingestion/NumCtxSlider';
import type { HardwareInfoApi } from './ingestion/hardware-fit';

interface FirstBootModelBannerProps {
  smartModel?: string;
  vramGb?: number;
}

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

function HardwareRecommendationBanner({ recommendation }: { recommendation: HardwareRecommendation }) {
  return (
    <div
      className="mb-3 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs dark:border-blue-800 dark:bg-blue-950"
      data-testid="hw-recommendation-banner"
    >
      <p className="font-medium text-blue-900 dark:text-blue-100">{recommendation.summary}</p>
      <p className="mt-0.5 text-blue-700 dark:text-blue-300">
        Advisory — this does not change an active route automatically.
      </p>
      {recommendation.aliases.length > 0 && (
        <ul className="mt-2 space-y-1" data-testid="hw-recommendation-alias-list">
          {recommendation.aliases.map((entry) => (
            <li key={entry.alias} className="flex flex-wrap items-center gap-x-2 text-blue-800 dark:text-blue-200">
              <span className="font-medium">{entry.alias}</span>
              <span className="text-blue-600 dark:text-blue-400">uses</span>
              <span className="font-mono">{entry.model}</span>
              {entry.notes && <span className="text-blue-600 dark:text-blue-400">{entry.notes}</span>}
              {entry.confirm_on_target && (
                <span
                  className="rounded-sm border border-blue-300 px-1.5 py-0.5 text-[10px] font-medium dark:border-blue-700"
                  data-testid={`confirm-on-target-${entry.alias}`}
                >
                  Confirm on this machine
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type GenerativeRole = 'fast' | 'smart';

const ROLE_COPY: Record<GenerativeRole, { key: string; label: string; description: string }> = {
  fast: {
    key: 'llm.fast_model',
    label: 'Quick model',
    description: 'Scores and triages incoming papers. Your choice applies automatically.',
  },
  smart: {
    key: 'llm.smart_model',
    label: 'Main model',
    description: 'Writes summaries, cards, extraction, and Ask answers. Your choice applies automatically.',
  },
};

function providerForRoute(
  entry: ModelCatalogEntry | undefined,
  value: string,
  providers: ProviderMetadata[],
): ProviderMetadata | undefined {
  if (entry) {
    return isLocalModel(entry)
      ? undefined
      : providers.find((provider) => provider.id === entry.provider);
  }
  return providers.find((provider) => value.startsWith(provider.assignment_prefix));
}

function availabilityLabel(
  entry: ModelCatalogEntry | undefined,
  provider: ProviderMetadata | undefined,
  listStatus: ProviderModelListStatus | undefined,
): string {
  if (!entry) {
    if (provider && listStatus?.error) return 'Provider catalog is currently unavailable';
    if (provider) return 'Configured cloud model; catalog details unavailable';
    return 'Model details unavailable';
  }
  if (isLocalModel(entry)) {
    const fit = entry.fit_detail?.default;
    if (fit === 'unfit') return 'Does not fit this machine at the current reading window';
    if (fit === 'partial') return 'Fits with a smaller reading window';
    if (fit === 'fits') return 'Fits this machine';
    return entry.pulled ? 'Installed on this machine' : 'Available to install';
  }
  if (listStatus?.error) return 'Provider catalog is currently unavailable';
  if (listStatus?.fetched_at) return `Provider catalog checked ${new Date(listStatus.fetched_at).toLocaleString()}`;
  return 'Configured provider; connection not checked yet';
}

function boundaryLabel(entry: ModelCatalogEntry | undefined, provider: ProviderMetadata | undefined): string {
  if (entry && isLocalModel(entry)) return 'Local — stays on this machine';
  if (provider?.kind === 'router') return `Cloud — through ${provider.display_name}`;
  if (provider) return `Cloud — direct to ${provider.display_name}`;
  return 'Unknown — route details unavailable';
}

function RouteDetails({ rows }: { rows: Array<{ label: string; value: string }> }) {
  return (
    <dl className="grid gap-x-4 gap-y-1 border-y border-hair py-2 text-xs sm:grid-cols-[7rem_minmax(0,1fr)]">
      {rows.map((row) => (
        <div key={row.label} className="contents">
          <dt className="font-mono text-muted-foreground">{row.label}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

interface LlmRouteCardProps {
  role: GenerativeRole;
  value: string;
  entry?: ModelCatalogEntry;
  provider?: ProviderMetadata;
  listStatus?: ProviderModelListStatus;
  machineId: string;
  hardware?: HardwareInfoApi;
  configs: ConfigEntry[];
  deliveryStatus?: 'pending_restart' | 'applied';
  isPending: boolean;
  error: string | null;
  onSave: (key: string, value: string) => void;
  initialPickerSource?: string;
  openPickerOnMount?: boolean;
}

function LlmRouteCard({
  role,
  value,
  entry,
  provider,
  listStatus,
  machineId,
  hardware,
  configs,
  deliveryStatus,
  isPending,
  error,
  onSave,
  initialPickerSource,
  openPickerOnMount = false,
}: LlmRouteCardProps) {
  const [configureOpen, setConfigureOpen] = useState(false);
  const copy = ROLE_COPY[role];
  const local = Boolean(entry && isLocalModel(entry));
  const cloud = Boolean(provider);
  return (
    <Card className="rounded-md border-hair shadow-none" data-testid={`llm-route-card-${role}`}>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <h4 className="text-sm font-semibold">{copy.label}</h4>
              {deliveryStatus === 'pending_restart' && (
                <span className="text-xs font-medium text-amber-700 dark:text-amber-400" data-testid={`delivery-pending-${role}`}>
                  pending — applying automatically when the model service recovers
                </span>
              )}
            </div>
            <p className="text-xs text-muted-foreground">{copy.description}</p>
          </div>
          <span className="font-mono text-xs text-muted-foreground">{value || 'Not configured'}</span>
        </div>

        <RouteDetails rows={[
          { label: 'Boundary', value: boundaryLabel(entry, provider) },
          { label: 'Availability', value: availabilityLabel(entry, provider, listStatus) },
          {
            label: 'Data handling',
            value: local
              ? 'Prompts and research text stay on this machine'
              : provider?.data_note ?? 'Data handling is unavailable for this route',
          },
          {
            label: 'Price',
            value: local ? 'No provider charge' : cloud && entry ? modelPriceLabel(entry) : 'Price unavailable',
          },
        ]} />

        <div className="flex flex-wrap items-center justify-between gap-2">
          <ModelSelector
            value={value}
            onChange={(model) => onSave(copy.key, model)}
            configKey={copy.key}
            initialSource={initialPickerSource}
            defaultOpen={openPickerOnMount}
          />
          {!local && provider && (
            <a
              className="text-xs text-primary underline-offset-4 hover:underline"
              href={`/settings?section=models&item=providers&provider=${encodeURIComponent(provider.id)}`}
            >
              Provider details
            </a>
          )}
        </div>

        {local && (
          <div>
            <button
              type="button"
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => setConfigureOpen((open) => !open)}
              data-testid={`configure-toggle-${role}`}
              disabled={isPending}
              aria-expanded={configureOpen}
            >
              {configureOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              Local model controls
            </button>
            {configureOpen && (
              <NumCtxSlider
                role={role}
                machineId={machineId}
                fitDetail={entry?.fit_detail}
                hardware={hardware}
                modelId={value}
                supportsThinking={entry?.supports_thinking ?? false}
                configs={configs}
              />
            )}
          </div>
        )}
        {error && (
          <p role="alert" className="text-xs text-destructive" data-testid={`config-save-error-${copy.key}`}>
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function EmbeddingRouteCard({
  model,
  dimension,
}: {
  model: string;
  dimension: number | null;
}) {
  return (
    <Card className="rounded-md border-hair shadow-none" data-testid="llm-route-card-embed">
      <CardContent className="space-y-3 p-4">
        <div>
          <h4 className="text-sm font-semibold">Embedding model</h4>
          <p className="text-xs text-muted-foreground">Builds the searchable representation of every paper.</p>
        </div>
        <p className="font-mono text-xs">{model || 'Runtime model unavailable'}</p>
        <RouteDetails rows={[
          { label: 'Boundary', value: 'Local — index compatibility is fixed for this deployment' },
          { label: 'Dimension', value: dimension == null ? 'Unavailable' : `${dimension.toLocaleString()} values per vector` },
          { label: 'Data handling', value: 'Paper text stays on this machine during embedding' },
          { label: 'Change safety', value: 'Changing model or dimension requires re-embedding the whole index' },
        ]} />
        <details className="text-xs">
          <summary className="cursor-pointer font-medium">Why is this model locked?</summary>
          <p className="mt-2 text-muted-foreground">
            Existing vectors must have the same meaning and dimension as new vectors. A deliberate migration backs up and rebuilds the index.
          </p>
        </details>
        <a
          className="inline-block text-xs text-primary underline-offset-4 hover:underline"
          href={docsUrl('manual/changing-embedding-model.md')}
          target="_blank"
          rel="noopener noreferrer"
        >
          Read the embedding model migration guide
        </a>
      </CardContent>
    </Card>
  );
}

const HIDE_FROM_UI = new Set([
  'setup.completed',
  'telegram.owner_chat_id',
  'pulse.cron',
  'pulse.enabled',
  'pulse.deck_size',
  'pulse.stage2_top_k',
  'pulse.weights',
  'user.timezone',
]);

const CONFIG_METADATA: Record<string, {
  label: string;
  description: string;
  group: string;
  tooltip?: string;
  type?: 'boolean' | 'number' | 'string';
  min?: number;
  max?: number;
  step?: number;
}> = {
  'fsrs.desired_retention': {
    label: 'Target Retention',
    description: 'Desired probability of recalling a card correctly (0.0–1.0)',
    group: 'Spaced Repetition',
    tooltip: 'Desired recall probability at review time. Higher values create more frequent reviews.',
    type: 'number',
    min: 0.7,
    max: 1,
    step: 0.01,
  },
  'fsrs.learning_steps': {
    label: 'Learning Steps',
    description: 'Steps before a card graduates, as [min, max] minutes',
    group: 'Spaced Repetition',
    tooltip: 'Minutes between a new card’s first review attempts before long-term scheduling.',
  },
};

interface IngestionSectionProps {
  filterGroups?: string[];
  modelPickerRequest?: {
    role: GenerativeRole;
    provider: string;
  };
}

function configString(configs: ConfigEntry[], key: string): string {
  const value = configs.find((entry) => entry.key === key)?.value;
  return typeof value === 'string' ? value.replace(/^"|"$/g, '') : '';
}

export function IngestionSection({ filterGroups, modelPickerRequest }: IngestionSectionProps = {}) {
  const queryClient = useQueryClient();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [saveError, setSaveError] = useState<{ key: string; message: string } | null>(null);
  const wantsModels = filterGroups === undefined || filterGroups.includes('AI models');

  const { data: configs = [], isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });
  const { data: systemModels } = useQuery({
    queryKey: QUERY_KEYS.config.systemModels(),
    queryFn: ({ signal }) => fetchSystemModels(signal),
    staleTime: 60_000,
  });
  const { data: providers = [] } = useQuery({
    queryKey: ['settings', 'providers'],
    queryFn: listProviders,
    enabled: wantsModels,
  });
  const setMutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() });
      setEditingKey(null);
      setSaveError(null);
    },
    onError: (error: Error, variables) => setSaveError({ key: variables.key, message: `Failed to save: ${error.message}` }),
  });

  if (isLoading) return <div className="py-8 text-center text-muted-foreground">Loading config...</div>;
  if (isError) return <p className="py-8 text-center text-sm text-destructive" role="alert" data-testid="config-load-error">Failed to load configuration. Check service health and try again.</p>;

  const catalog = systemModels?.catalog ?? [];
  const hardware = systemModels?.hardware;
  const machineId = hardware?.machine_id ?? 'local';
  const modelValue = (role: GenerativeRole) =>
    configString(configs, ROLE_COPY[role].key) || systemModels?.current?.[`${role}_model`] || '';
  const catalogEntry = (role: GenerativeRole, value: string) =>
    catalog.find((entry) => entry.roles.includes(role) && matchesModelId(entry, value));
  const currentSmartModel = systemModels?.current?.smart_model ?? '';
  const showFirstBootBanner = Boolean(currentSmartModel) && (hardware?.vram_gb ?? 0) > 0;

  const genericEntries = configs.filter((entry) => {
    if (HIDE_FROM_UI.has(entry.key) || entry.key.startsWith('llm.')) return false;
    const group = CONFIG_METADATA[entry.key]?.group;
    return group != null && (filterGroups === undefined || filterGroups.includes(group));
  });

  const startEdit = (entry: ConfigEntry) => {
    setEditingKey(entry.key);
    setEditValue(typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value));
  };
  const saveEdit = () => {
    if (!editingKey) return;
    let value: unknown = editValue;
    try { value = JSON.parse(editValue); } catch { /* Keep ordinary strings as entered. */ }
    setMutation.mutate({ key: editingKey, value });
  };

  const embedding = systemModels?.embedding_contract;
  const fallbackEmbedding = catalog.find((entry) => entry.roles.includes('embed'));
  const embeddingModel = embedding?.model || systemModels?.current?.embed_model || fallbackEmbedding?.id || '';
  const embeddingDimension = embedding?.dimension ?? fallbackEmbedding?.embedding_dimension ?? null;

  return (
    <div className="space-y-4">
      {wantsModels && (
        <section aria-labelledby="ai-model-routes-heading" className="space-y-3">
          <div>
            <h3 id="ai-model-routes-heading" className="text-sm font-semibold text-muted-foreground">AI models</h3>
            <p className="mt-1 text-sm text-muted-foreground" data-testid="llm-models-description">
              Choose which models handle fast triage and deeper research. JARVIS applies your choices automatically. Local routes stay on this machine; cloud routes send selected research text to their provider.
            </p>
          </div>
          {hardware && <HardwareStrip hardware={hardware} />}
          {showFirstBootBanner ? (
            <FirstBootModelBanner smartModel={currentSmartModel} vramGb={hardware?.vram_gb} />
          ) : systemModels?.hardware_recommendation ? (
            <HardwareRecommendationBanner recommendation={systemModels.hardware_recommendation} />
          ) : null}
          <div className="grid gap-3 xl:grid-cols-2">
            {(['fast', 'smart'] as const).map((role) => {
              const value = modelValue(role);
              const entry = catalogEntry(role, value);
              const provider = providerForRoute(entry, value, providers);
              return (
                <LlmRouteCard
                  key={role}
                  role={role}
                  value={value}
                  entry={entry}
                  provider={provider}
                  listStatus={provider ? systemModels?.provider_lists?.[provider.id] : undefined}
                  machineId={machineId}
                  hardware={hardware}
                  configs={configs}
                  deliveryStatus={systemModels?.delivery?.[role]}
                  isPending={setMutation.isPending}
                  error={saveError?.key === ROLE_COPY[role].key ? saveError.message : null}
                  onSave={(key, model) => setMutation.mutate({ key, value: model })}
                  initialPickerSource={modelPickerRequest?.provider}
                  openPickerOnMount={modelPickerRequest?.role === role}
                />
              );
            })}
          </div>
          <EmbeddingRouteCard model={embeddingModel} dimension={embeddingDimension} />
        </section>
      )}

      {genericEntries.length > 0 && (
        <section className="space-y-2">
          <h4 className="text-sm font-semibold text-muted-foreground">Spaced Repetition</h4>
          {genericEntries.map((entry) => (
            <ConfigEntryCard
              key={entry.key}
              entry={entry}
              meta={CONFIG_METADATA[entry.key]}
              editingKey={editingKey}
              editValue={editValue}
              saveError={saveError?.key === entry.key ? saveError.message : null}
              isMutPending={setMutation.isPending}
              onMutate={(key, value) => setMutation.mutate({ key, value })}
              onStartEdit={startEdit}
              onEditValueChange={setEditValue}
              onSaveEdit={saveEdit}
              onCancelEdit={() => setEditingKey(null)}
            />
          ))}
        </section>
      )}
      {!wantsModels && genericEntries.length === 0 && (
        <p className="py-6 text-center text-sm text-muted-foreground">No settings are available in this section yet.</p>
      )}
    </div>
  );
}

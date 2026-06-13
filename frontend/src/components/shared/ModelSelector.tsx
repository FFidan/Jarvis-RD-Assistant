import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { ChevronDown, ChevronRight, Cpu, Download, Trash2 } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { useConfirm } from '@/hooks/use-confirm';
import { apiFetch, fetchSystemModels } from '@/lib/api';
import type { SystemModelsResponse } from '@/lib/api';
import type { ModelFitDetail } from '@/types';

/**
 * Local refinement of `SystemModelsResponse` that narrows `catalog` and `hardware`
 * to their concrete typed shapes used by this component.
 * Derived from the canonical type so it remains structurally consistent.
 */
type SystemModels = Omit<SystemModelsResponse, 'catalog' | 'hardware' | 'installed' | 'status' | 'current' | 'issues'> & {
  status: 'ok' | 'degraded';
  current: Record<string, string>;
  issues: Record<string, string>;
  catalog?: ModelCatalogEntry[];
  hardware?: HardwareInfo;
  installed?: unknown[];
};

interface ModelSelectorProps {
  value: string;
  onChange: (value: string) => void;
  configKey?: string;
}

interface HardwareInfo {
  vram_gb?: number;
  vram_source?: string;
  tier?: number;
  detected_at?: string;
  ollama_running?: number;
  /** Stable machine identifier (hostname). Used as key segment only — never displayed. */
  machine_id?: string;
}

type ModelRole = 'smart' | 'fast' | 'embed';
type ModelStatus =
  | 'active'
  | 'pulled'
  | 'downloadable'
  | 'unfit'
  | 'cloud_active'
  | 'cloud_required';

interface ModelCatalogEntry {
  id: string;
  name: string;
  provider: string;
  ollama_tag: string | null;
  roles: string[];
  vram_gb: number;
  disk_gb: number;
  context_tokens: number;
  license: string;
  tier: number;
  description: string;
  notes: string;
  last_reviewed: string;
  status: ModelStatus;
  active: boolean;
  pulled: boolean;
  provider_key_present: boolean;
  can_assign?: boolean;
  assign_blocker?: string | null;
  fit: string;
  size?: number;
  quantization?: string;
  /** Optional — backend T3-B populates this; older backends omit it. UI degrades gracefully. */
  fit_detail?: ModelFitDetail;
  /** True for thinking-capable models (Qwen3 family). T3-A populates this. */
  supports_thinking?: boolean;
}

// ---------------------------------------------------------------------------
// Fit-detail helpers (Contract 06 §4 — mirrored from IngestionSection)
// ---------------------------------------------------------------------------

/** Find the largest snap-step (power of 2) that stays within 85% VRAM threshold. */
function largestFittingCtxForEntry(fitDetail: ModelFitDetail, vramGb: number): number {
  const STOPS = [2048, 4096, 8192, 16384, 32768, 65536];
  let best: number = STOPS[0] ?? 2048;
  for (const stop of STOPS) {
    if (stop > fitDetail.max_num_ctx) break;
    const kvBytes = fitDetail.kv_cache_bytes_per_token ?? 1024;
    const required = (fitDetail.required_vram_gb ?? 0) + (Math.max(0, stop - fitDetail.default_num_ctx) * kvBytes) / 1e9;
    if (required <= vramGb * 0.85) best = stop;
  }
  return best;
}

/**
 * Effective fit string for a catalog entry: prefer fit_detail.default (populated by
 * T3-B backend) so the pull-CTA predicate uses the same VRAM-aware value as the
 * row-disable logic. Falls back to entry.fit for older backends that omit fit_detail.
 */
const effectiveFit = (e: ModelCatalogEntry): string => e.fit_detail?.default ?? e.fit;

// ---------------------------------------------------------------------------

const PROVIDER_LABELS: Record<string, string> = {
  local: 'Local (Ollama)',
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  google: 'Google',
};

function roleFromConfigKey(configKey?: string): ModelRole | undefined {
  const role = configKey?.replace(/^llm\./, '').replace(/_model$/, '');
  return role === 'smart' || role === 'fast' || role === 'embed' ? role : undefined;
}

function providerGroup(entry: ModelCatalogEntry): string {
  return entry.provider === 'ollama' ? 'local' : entry.provider;
}

function isLocalModel(entry: ModelCatalogEntry): boolean {
  return entry.provider === 'ollama' || Boolean(entry.ollama_tag);
}

function isEntrySelectableForRole(entry: ModelCatalogEntry, role?: ModelRole): boolean {
  if (role && !entry.roles.includes(role)) return false;
  if (typeof entry.can_assign === 'boolean') return entry.can_assign;
  if (isLocalModel(entry)) {
    return entry.status !== 'unfit' && (entry.active || entry.pulled || entry.status === 'active');
  }
  return entry.provider_key_present || entry.active || entry.status === 'cloud_active';
}

function isEntryVisibleForRole(entry: ModelCatalogEntry, role?: ModelRole): boolean {
  if (role && !entry.roles.includes(role)) return false;
  return isLocalModel(entry) || isEntrySelectableForRole(entry, role);
}

function assignmentBlocker(entry: ModelCatalogEntry, role?: ModelRole): string | null {
  if (role && !entry.roles.includes(role)) return 'Not available for this model role.';
  if (entry.assign_blocker) return entry.assign_blocker;
  if (typeof entry.can_assign === 'boolean') return entry.can_assign ? null : 'Not assignable.';
  if (isLocalModel(entry)) {
    if (entry.status === 'unfit') return 'Requires more VRAM.';
    if (!entry.active && !entry.pulled) return 'Pull this model before assigning it.';
  }
  if (!entry.provider_key_present && !entry.active && entry.status !== 'cloud_active') {
    return `Add a ${PROVIDER_LABELS[entry.provider] ?? entry.provider} API key before assigning this model.`;
  }
  return null;
}

function currentModelForRole(current: Record<string, string> | undefined, role?: ModelRole): string {
  if (!current || !role) return '';
  return current[`${role}_model`] ?? current[role] ?? '';
}

function normalizeLocalTag(value: string): string {
  return value.replace(/:latest$/, '');
}

function matchesConfiguredValue(entry: ModelCatalogEntry, value: string): boolean {
  if (!value) return false;
  const candidates = [entry.id, entry.ollama_tag].filter(
    (candidate): candidate is string => typeof candidate === 'string' && candidate.length > 0,
  );
  return candidates.some(
    (candidate) => candidate === value || normalizeLocalTag(candidate) === normalizeLocalTag(value),
  );
}

function localModelTag(entry: ModelCatalogEntry): string {
  return entry.ollama_tag ?? entry.id;
}

function localModelPath(entry: ModelCatalogEntry): string {
  return encodeURIComponent(localModelTag(entry));
}

export function ModelSelector({ value, onChange, configKey: role }: ModelSelectorProps) {
  const queryClient = useQueryClient();
  const { isOpen: deleteIsOpen, confirm: confirmDelete, handleConfirm: handleDeleteConfirm, handleCancel: handleDeleteCancel } = useConfirm();
  const { isOpen: pullIsOpen, confirm: confirmPull, handleConfirm: handlePullConfirm, handleCancel: handlePullCancel } = useConfirm();
  const [deleteTarget, setDeleteTarget] = useState<ModelCatalogEntry | null>(null);
  const [pullTarget, setPullTarget] = useState<ModelCatalogEntry | null>(null);
  const { data, error } = useQuery<SystemModels>({
    queryKey: QUERY_KEYS.config.systemModels(),
    queryFn: ({ signal }) => fetchSystemModels<SystemModels>(signal),
    staleTime: 60_000,
  });

  const catalog = data?.catalog ?? [];
  const currentRole = roleFromConfigKey(role);
  const systemDefault = currentModelForRole(data?.current, currentRole);
  const allModels = catalog.filter((entry) => isEntryVisibleForRole(entry, currentRole));
  const selectedEntry =
    allModels.find((entry) => matchesConfiguredValue(entry, value)) ??
    allModels.find((entry) => matchesConfiguredValue(entry, systemDefault));
  const effectiveValue = selectedEntry?.id ?? '';
  const issues = Object.values(data?.issues ?? {}).filter(Boolean);
  const emptyStateMessage = error
    ? 'Could not load models. Check the API and Ollama status.'
    : issues[0] ?? 'No models found. Is Ollama running?';
  const emptyStateContent =
    catalog.length > 0
      ? 'No compatible models available for this role'
      : issues.length > 0
        ? 'No models available.'
        : emptyStateMessage;

  const formatSize = (bytes: number) => {
    if (bytes > 1e9) return `${(bytes / 1e9).toFixed(1)}GB`;
    if (bytes > 1e6) return `${(bytes / 1e6).toFixed(0)}MB`;
    return `${bytes}B`;
  };

  const formatGb = (value: number) => {
    if (value <= 0) return '';
    return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}GB`;
  };

  const hardwareSummary = (hardware: HardwareInfo | undefined) => {
    if (!hardware) return null;
    const parts: ReactNode[] = [];
    if (typeof hardware.vram_gb === 'number') parts.push(`${formatGb(hardware.vram_gb)} VRAM`);
    if (typeof hardware.tier === 'number') parts.push(`Tier ${hardware.tier}`);
    if (hardware.vram_source === 'macos-approx') parts.push('approximate');
    return parts.length > 0 ? parts : null;
  };

  const statusLabel = (entry: ModelCatalogEntry, isCurrent: boolean) => {
    if (isCurrent) return 'current';
    if (entry.status === 'pulled') return 'pulled';
    if (entry.status === 'downloadable') return 'downloadable';
    return '';
  };

  const providerOrder = ['local', 'anthropic', 'openai', 'google'];
  const groups = Array.from(new Set(allModels.map(providerGroup))).sort((a, b) => {
    const ia = providerOrder.indexOf(a);
    const ib = providerOrder.indexOf(b);
    const oa = ia === -1 ? providerOrder.length : ia;
    const ob = ib === -1 ? providerOrder.length : ib;
    return oa - ob || a.localeCompare(b);
  });

  const groupedModels = groups
    .map((group) => ({
      group,
      models: allModels.filter((model) => providerGroup(model) === group),
    }))
    .filter((group) => group.models.length > 0);
  const detectedHardware = hardwareSummary(data?.hardware);
  const pullableModels = allModels.filter(
    (entry) =>
      isLocalModel(entry) &&
      entry.status === 'downloadable' &&
      effectiveFit(entry) !== 'unfit',
  );
  const pullMutation = useMutation({
    mutationFn: (entry: ModelCatalogEntry) =>
      apiFetch(`/api/system/models/${localModelPath(entry)}/pull`, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() }),
  });
  const deleteMutation = useMutation({
    mutationFn: (entry: ModelCatalogEntry) =>
      apiFetch(`/api/system/models/${localModelPath(entry)}`, { method: 'DELETE' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() }),
  });
  const [pullingIds, setPullingIds] = useState<Set<string>>(new Set());
  const [manageOpen, setManageOpen] = useState(false);
  const canDeleteSelected =
    selectedEntry !== undefined &&
    isLocalModel(selectedEntry) &&
    selectedEntry.pulled &&
    !selectedEntry.active &&
    selectedEntry.status !== 'active';
  const deletableModels = (catalog ?? []).filter(
    (e) =>
      isLocalModel(e) &&
      e.pulled &&
      !e.active &&
      e.status !== 'active' &&
      e.id !== selectedEntry?.id,
  );
  const handlePull = async (entry: ModelCatalogEntry) => {
    setPullTarget(entry);
    const confirmed = await confirmPull();
    setPullTarget(null);
    if (!confirmed) return;
    setPullingIds((prev) => new Set(prev).add(entry.id));
    try {
      await pullMutation.mutateAsync(entry);
    } finally {
      setPullingIds((prev) => {
        const next = new Set(prev);
        next.delete(entry.id);
        return next;
      });
    }
  };
  const handleDelete = async (entry: ModelCatalogEntry) => {
    setDeleteTarget(entry);
    const confirmed = await confirmDelete();
    if (confirmed) {
      deleteMutation.mutate(entry);
    }
    setDeleteTarget(null);
  };

  const setupNeeded =
    selectedEntry !== undefined &&
    (selectedEntry.status === 'downloadable' || selectedEntry.status === 'unfit');
  const recommendedEntry = setupNeeded ? (pullableModels[0] ?? null) : null;
  const hardwareLabel = detectedHardware ? detectedHardware.join(' · ') : null;

  // Routing divergence: what LiteLLM actually serves vs what is saved.
  const routingMap = data?.routing as Record<string, string> | undefined;
  const savedModel = currentRole ? systemDefault : undefined;
  const routedModel = currentRole ? routingMap?.[currentRole] : undefined;
  const isDiverged =
    savedModel != null &&
    savedModel !== '' &&
    routedModel != null &&
    routedModel !== savedModel;

  return (
    <div className="space-y-2">
      {setupNeeded && recommendedEntry && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-4 dark:border-amber-700 dark:bg-amber-950">
          <p className="font-semibold text-amber-900 dark:text-amber-100">Setup needed</p>
          <p className="text-sm text-amber-800 dark:text-amber-200">
            <strong>{selectedEntry?.id}</strong> is not pulled yet.
            {hardwareLabel && ` Recommended for your hardware (${hardwareLabel}):`}{' '}
            <strong>{recommendedEntry.name}</strong>
          </p>
          <Button
            type="button"
            size="sm"
            className="mt-2"
            onClick={() => handlePull(recommendedEntry)}
            disabled={pullingIds.has(recommendedEntry.id)}
          >
            <Download className="mr-2 h-4 w-4" />
            Pull {recommendedEntry.name} to get started
          </Button>
        </div>
      )}
      <Select value={effectiveValue} onValueChange={onChange}>
        <SelectTrigger>
          <SelectValue placeholder="Select a model" />
        </SelectTrigger>
        <SelectContent>
          {detectedHardware && (
            <div className="px-2 pb-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Detected hardware</span>
              <span className="ml-2 inline-flex flex-wrap gap-x-1">
                {detectedHardware.map((part, index) => (
                  <span key={String(part)}>
                    {index > 0 && <span aria-hidden="true">· </span>}
                    {part}
                  </span>
                ))}
              </span>
            </div>
          )}
          {issues.map((issue) => (
            <div key={issue} className="px-2 pb-2 text-xs text-amber-700">
              {issue}
            </div>
          ))}
          {allModels.length === 0 ? (
            <div className="px-2 py-4 text-center text-sm text-muted-foreground">
              <Cpu className="mx-auto mb-2 h-5 w-5" />
              {emptyStateContent}
            </div>
          ) : (
            groupedModels.map(({ group, models: groupModels }, index) => (
              <SelectGroup key={group}>
                {index > 0 && <SelectSeparator />}
                <SelectLabel>{PROVIDER_LABELS[group] ?? group}</SelectLabel>
                {groupModels.map((m) => {
                  const blocker = assignmentBlocker(m, currentRole);
                  const canAssign = blocker === null && isEntrySelectableForRole(m, currentRole);
                  const isCurrent =
                    m.active ||
                    m.status === 'active' ||
                    m.status === 'cloud_active' ||
                    matchesConfiguredValue(m, systemDefault) ||
                    matchesConfiguredValue(m, value);
                  const badge = statusLabel(m, isCurrent);

                  // fit_detail-based disabling (Contract 06 §6, §10.3)
                  const fitDefault = m.fit_detail?.default;
                  const isUnfitByDetail = fitDefault === 'unfit';
                  const isCloud = fitDefault === 'cloud' || m.provider !== 'ollama';
                  // A model is disabled if canAssign=false OR fit_detail says unfit
                  const isDisabled = !canAssign || isUnfitByDetail;

                  // Tooltip for unfit via fit_detail
                  const vramGb = data?.hardware?.vram_gb ?? 0;
                  const unfitTooltip = isUnfitByDetail && m.fit_detail && vramGb > 0
                    ? `Won't fit at current num_ctx — try ${largestFittingCtxForEntry(m.fit_detail, vramGb).toLocaleString()} tokens`
                    : undefined;

                  const itemContent = (
                    <SelectItem key={m.id} value={m.id} disabled={isDisabled}>
                      <div className="space-y-1">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span>{m.name}</span>
                          {m.id !== m.name && (
                            <span className="text-xs text-muted-foreground">
                              {m.id}
                            </span>
                          )}
                          {m.quantization && (
                            <span className="text-xs text-muted-foreground">
                              {m.quantization}
                            </span>
                          )}
                          {m.size !== undefined && m.size > 0 && (
                            <span className="text-xs text-muted-foreground">
                              ({formatSize(m.size)})
                            </span>
                          )}
                          {m.size === undefined && m.disk_gb > 0 && (
                            <span className="text-xs text-muted-foreground">
                              ({formatGb(m.disk_gb)} disk)
                            </span>
                          )}
                          {m.vram_gb > 0 && (
                            <span className="text-xs text-muted-foreground">
                              {formatGb(m.vram_gb)} VRAM
                            </span>
                          )}
                          <span className="text-xs text-muted-foreground">
                            Requires Tier {m.tier}
                          </span>
                          {badge && (
                            <span className="text-xs font-medium text-green-600">
                              {badge}
                            </span>
                          )}
                          {/* fit_detail badges (Contract 06 §6.1) */}
                          {isCloud && fitDefault === 'cloud' && (
                            <span className="text-xs rounded-full bg-muted px-1.5 py-0.5 text-muted-foreground">
                              Cloud
                            </span>
                          )}
                          {fitDefault === 'unknown' && (
                            <span className="text-xs text-muted-foreground">?</span>
                          )}
                        </div>
                        {blocker && !unfitTooltip && (
                          <div className="text-xs text-amber-700">
                            {blocker}
                          </div>
                        )}
                        {unfitTooltip && (
                          <div className="text-xs text-red-600">
                            {unfitTooltip}
                          </div>
                        )}
                      </div>
                    </SelectItem>
                  );

                  // Wrap in Tooltip when there's an unfit message
                  if (unfitTooltip) {
                    return (
                      <TooltipProvider key={m.id}>
                        <Tooltip>
                          <TooltipTrigger asChild>{itemContent}</TooltipTrigger>
                          <TooltipContent>{unfitTooltip}</TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    );
                  }
                  return itemContent;
                })}
              </SelectGroup>
            ))
          )}
        </SelectContent>
      </Select>
      {isDiverged && (
        <p
          className="text-xs text-amber-700 dark:text-amber-400"
          data-testid={`routing-diverged-${currentRole}`}
        >
          Saved: {savedModel} · currently serving: {routedModel}
        </p>
      )}
      {pullableModels.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {pullableModels.map((entry) => (
            <Button
              key={`pull-${entry.id}`}
              type="button"
              size="sm"
              variant="outline"
              onClick={() => handlePull(entry)}
              disabled={pullingIds.has(entry.id)}
              aria-label={`Pull model ${entry.name}`}
            >
              <Download className="mr-2 h-4 w-4" />
              Pull {entry.name}
            </Button>
          ))}
        </div>
      )}
      {((canDeleteSelected && selectedEntry) || deletableModels.length > 0) && (
        <div className="border-t pt-2">
          <button
            type="button"
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            onClick={() => setManageOpen((prev) => !prev)}
            aria-expanded={manageOpen}
          >
            {manageOpen ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            Manage installed models
          </button>
          {manageOpen && (
            <div className="mt-2 flex flex-wrap gap-2">
              {canDeleteSelected && selectedEntry && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => handleDelete(selectedEntry)}
                  disabled={deleteMutation.isPending || !!deleteTarget}
                  aria-label={`Delete model ${selectedEntry.name}`}
                >
                  <Trash2 className="mr-2 h-4 w-4" />
                  Delete {selectedEntry.name}
                </Button>
              )}
              {deletableModels.map((entry) => (
                <Button
                  key={`delete-${entry.id}`}
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => handleDelete(entry)}
                  disabled={deleteMutation.isPending || !!deleteTarget}
                  aria-label={`Delete model ${entry.name}`}
                >
                  <Trash2 className="mr-2 h-4 w-4" />
                  Delete {entry.name}
                </Button>
              ))}
            </div>
          )}
        </div>
      )}
      <ConfirmDialog
        open={deleteIsOpen && deleteTarget !== null}
        title="Delete Model"
        description={
          deleteTarget
            ? `This will remove ${deleteTarget.name} from Ollama. You can pull it again later.`
            : undefined
        }
        confirmLabel="Delete"
        onConfirm={handleDeleteConfirm}
        onCancel={handleDeleteCancel}
      />
      <ConfirmDialog
        open={pullIsOpen && pullTarget !== null}
        title="Pull Model"
        description={
          pullTarget
            ? `Pull ${pullTarget.name}?${
                [
                  pullTarget.disk_gb > 0 ? `${pullTarget.disk_gb.toFixed(1)} GB disk` : '',
                  pullTarget.vram_gb > 0 ? `${pullTarget.vram_gb.toFixed(1)} GB VRAM` : '',
                ]
                  .filter(Boolean)
                  .join(', ')
                  ? ` (requires ${[
                      pullTarget.disk_gb > 0 ? `${pullTarget.disk_gb.toFixed(1)} GB disk` : '',
                      pullTarget.vram_gb > 0 ? `${pullTarget.vram_gb.toFixed(1)} GB VRAM` : '',
                    ]
                      .filter(Boolean)
                      .join(', ')})`
                  : ''
              }`
            : undefined
        }
        confirmLabel="Pull"
        onConfirm={handlePullConfirm}
        onCancel={handlePullCancel}
      />
    </div>
  );
}

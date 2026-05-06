import { useQuery } from '@tanstack/react-query';
import { Cpu } from 'lucide-react';
import type { ReactNode } from 'react';
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
import { apiFetch } from '@/lib/api';

interface SystemModels {
  status: 'ok' | 'degraded';
  installed: unknown[];
  hardware: HardwareInfo;
  current: Record<string, string>;
  issues: Record<string, string>;
  catalog: ModelCatalogEntry[];
  recommendations?: Record<string, unknown>;
}

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
}

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

export function ModelSelector({ value, onChange, configKey: role }: ModelSelectorProps) {
  const { data, error } = useQuery<SystemModels>({
    queryKey: ['system-models'],
    queryFn: () => apiFetch<SystemModels>('/api/system/models'),
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

  return (
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
                return (
                  <SelectItem key={m.id} value={m.id} disabled={!canAssign}>
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
                      </div>
                      {blocker && (
                        <div className="text-xs text-amber-700">
                          {blocker}
                        </div>
                      )}
                    </div>
                  </SelectItem>
                );
              })}
            </SelectGroup>
          ))
        )}
      </SelectContent>
    </Select>
  );
}

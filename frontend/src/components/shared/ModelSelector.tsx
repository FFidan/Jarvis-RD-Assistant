import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, Download, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { ModelPickerDialog } from '@/components/shared/model-picker/ModelPickerDialog';
import {
  isLocalModel,
  matchesModelId,
  type GenerativeModelRole,
} from '@/components/shared/model-picker/model-options';
import { Button } from '@/components/ui/button';
import { useConfirm } from '@/hooks/use-confirm';
import { apiFetchVoid, cloudProviderLabel, fetchSystemModels } from '@/lib/api';
import type { ModelCatalogEntry } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';

interface ModelSelectorProps {
  value: string;
  onChange: (value: string) => void;
  configKey?: string;
  initialSource?: string;
  defaultOpen?: boolean;
}

type ModelRole = GenerativeModelRole | 'embed';

function roleFromConfigKey(configKey?: string): ModelRole | undefined {
  const role = configKey?.replace(/^llm\./, '').replace(/_model$/, '');
  return role === 'smart' || role === 'fast' || role === 'embed' ? role : undefined;
}

function isOllamaManaged(entry: ModelCatalogEntry): boolean {
  return entry.provider === 'ollama';
}

function isEntryVisibleForRole(entry: ModelCatalogEntry, role?: ModelRole): boolean {
  if (role && !entry.roles.includes(role)) return false;
  return isLocalModel(entry) || entry.provider !== 'ollama';
}

function assignmentBlocker(entry: ModelCatalogEntry, role?: ModelRole): string | null {
  if (role && !entry.roles.includes(role)) return 'Not available for this model role.';
  if (entry.assign_blocker) return entry.assign_blocker;
  if (typeof entry.can_assign === 'boolean') return entry.can_assign ? null : 'Not assignable.';
  if (isLocalModel(entry)) {
    if (entry.status === 'unfit' || entry.fit_detail?.default === 'unfit') return 'Requires more VRAM.';
    if (!entry.active && !entry.pulled) return 'Pull this model before assigning it.';
  }
  if (!entry.provider_key_present && !entry.active && entry.status !== 'cloud_active') {
    return `Add a ${cloudProviderLabel(entry.provider)} API key before assigning this model.`;
  }
  return null;
}

function currentModelForRole(current: Record<string, string> | undefined, role?: ModelRole): string {
  if (!current || !role) return '';
  return current[`${role}_model`] ?? current[role] ?? '';
}

function localModelPath(entry: ModelCatalogEntry): string {
  return encodeURIComponent(entry.ollama_tag ?? entry.id);
}

export function ModelSelector({
  value,
  onChange,
  configKey,
  initialSource,
  defaultOpen = false,
}: ModelSelectorProps) {
  const queryClient = useQueryClient();
  const currentRole = roleFromConfigKey(configKey);
  const [manageOpen, setManageOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ModelCatalogEntry | null>(null);
  const [pullTarget, setPullTarget] = useState<ModelCatalogEntry | null>(null);
  const [pullingIds, setPullingIds] = useState<Set<string>>(new Set());
  const deleteConfirm = useConfirm();
  const pullConfirm = useConfirm();
  const { data, error } = useQuery({
    queryKey: QUERY_KEYS.config.systemModels(),
    queryFn: ({ signal }) => fetchSystemModels(signal),
    staleTime: 60_000,
  });

  const allModels = (data?.catalog ?? []).filter((entry) =>
    isEntryVisibleForRole(entry, currentRole),
  );
  const systemDefault = currentModelForRole(data?.current, currentRole);
  const selectedEntry =
    allModels.find((entry) => matchesModelId(entry, value)) ??
    allModels.find((entry) => matchesModelId(entry, systemDefault));
  const selectedId = selectedEntry?.id ?? (value || systemDefault);
  const reviewedIds = new Set(
    currentRole ? (data?.reviewed_choices?.[currentRole] ?? []).map((entry) => entry.id) : [],
  );
  const localRoute = selectedEntry != null && isLocalModel(selectedEntry);
  const pullableModels = localRoute
    ? allModels.filter(
        (entry) =>
          isOllamaManaged(entry) &&
          entry.status === 'downloadable' &&
          entry.fit_detail?.default !== 'unfit',
      )
    : [];
  const deletableModels = localRoute
    ? allModels.filter(
        (entry) =>
          isOllamaManaged(entry) &&
          entry.pulled &&
          !entry.active &&
          entry.status !== 'active' &&
          entry.id !== selectedEntry?.id,
      )
    : [];
  const canDeleteSelected = Boolean(
    localRoute &&
      selectedEntry?.pulled &&
      !selectedEntry.active &&
      selectedEntry.status !== 'active',
  );
  const setupNeeded = Boolean(
    localRoute &&
      selectedEntry &&
      (selectedEntry.status === 'downloadable' || selectedEntry.status === 'unfit'),
  );
  const recommendedEntry = setupNeeded ? (pullableModels[0] ?? null) : null;

  const pullMutation = useMutation({
    mutationFn: (entry: ModelCatalogEntry) =>
      apiFetchVoid(`/api/system/models/${localModelPath(entry)}/pull`, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() }),
  });
  const deleteMutation = useMutation({
    mutationFn: (entry: ModelCatalogEntry) =>
      apiFetchVoid(`/api/system/models/${localModelPath(entry)}`, { method: 'DELETE' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() }),
  });

  const handlePull = async (entry: ModelCatalogEntry) => {
    setPullTarget(entry);
    const confirmed = await pullConfirm.confirm();
    setPullTarget(null);
    if (!confirmed) return;
    setPullingIds((previous) => new Set(previous).add(entry.id));
    try {
      await pullMutation.mutateAsync(entry);
    } finally {
      setPullingIds((previous) => {
        const next = new Set(previous);
        next.delete(entry.id);
        return next;
      });
    }
  };

  const handleDelete = async (entry: ModelCatalogEntry) => {
    setDeleteTarget(entry);
    const confirmed = await deleteConfirm.confirm();
    if (confirmed) deleteMutation.mutate(entry);
    setDeleteTarget(null);
  };

  const savedModel = currentRole ? systemDefault : undefined;
  const routedModel = currentRole ? data?.routing?.[currentRole] : undefined;
  const isDiverged = Boolean(savedModel && routedModel && routedModel !== savedModel);
  const issues = Object.values(data?.issues ?? {}).filter(Boolean);

  return (
    <div className="space-y-2">
      {setupNeeded && recommendedEntry && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-700 dark:bg-amber-950">
          <p className="font-semibold text-amber-900 dark:text-amber-100">Setup needed</p>
          <p className="text-amber-800 dark:text-amber-200">
            {selectedEntry?.name} is not installed. Recommended available model:{' '}
            <strong>{recommendedEntry.name}</strong>.
          </p>
        </div>
      )}

      {(currentRole === 'fast' || currentRole === 'smart') && allModels.length > 0 ? (
        <ModelPickerDialog
          role={currentRole}
          models={allModels}
          selectedId={selectedId}
          reviewedIds={reviewedIds}
          providerLists={data?.provider_lists ?? {}}
          blockerFor={(entry) => assignmentBlocker(entry, currentRole)}
          onSelect={onChange}
          initialSource={initialSource}
          defaultOpen={defaultOpen}
        />
      ) : null}

      {error && (
        <p role="alert" className="text-xs text-destructive">
          Could not load models. Check the API and model service status.
        </p>
      )}
      {!error && allModels.length === 0 && (
        <p className="text-xs text-muted-foreground">
          {issues[0] ?? 'No compatible models are available for this role.'}
        </p>
      )}
      {isDiverged && (
        <p className="text-xs text-amber-700 dark:text-amber-400" data-testid={`routing-diverged-${currentRole}`}>
          You selected &quot;{savedModel}&quot; but the model service is currently using &quot;{routedModel}&quot;.
        </p>
      )}

      {localRoute && (pullableModels.length > 0 || canDeleteSelected || deletableModels.length > 0) && (
        <div className="border-t pt-2">
          <button
            type="button"
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            onClick={() => setManageOpen((previous) => !previous)}
            aria-expanded={manageOpen}
          >
            {manageOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            Install &amp; manage local models
          </button>
          {manageOpen && (
            <div className="mt-2 flex flex-wrap gap-2">
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
              {canDeleteSelected && selectedEntry && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => handleDelete(selectedEntry)}
                  disabled={deleteMutation.isPending || deleteTarget != null}
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
                  disabled={deleteMutation.isPending || deleteTarget != null}
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
        open={deleteConfirm.isOpen && deleteTarget != null}
        title="Delete model"
        description={
          deleteTarget
            ? `This removes ${deleteTarget.name} from Ollama${
                deleteTarget.disk_gb > 0
                  ? ` and frees approximately ${deleteTarget.disk_gb.toFixed(1)} GB`
                  : ''
              }. You can pull it again later.`
            : undefined
        }
        confirmLabel="Delete"
        onConfirm={deleteConfirm.handleConfirm}
        onCancel={deleteConfirm.handleCancel}
      />
      <ConfirmDialog
        open={pullConfirm.isOpen && pullTarget != null}
        title="Pull model"
        description={pullTarget ? `Pull ${pullTarget.name} to this machine?` : undefined}
        confirmLabel="Pull"
        onConfirm={pullConfirm.handleConfirm}
        onCancel={pullConfirm.handleCancel}
      />
    </div>
  );
}

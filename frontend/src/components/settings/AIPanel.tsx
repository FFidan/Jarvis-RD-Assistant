/**
 * AIPanel — AI backend configuration panel.
 *
 * Shows hardware tier, configured vs observed backend, candidate list, and
 * allows the user to apply a new backend/model combination.
 *
 * GET  /api/settings/ai           → getAISettings()
 * POST /api/settings/ai           → postAISettings({ backend, model })
 * POST /api/settings/ai/redetect  → redetectHW()
 * GET  /api/setup/status          → getFirstRunStatus() (for hw_tier_changed banner)
 * POST /api/settings/ai/dismiss-banner → dismissBanner('hw_change')
 *
 * HW-change banner limitation (Phase-3): dismissing the banner writes a
 * system_events row but does NOT update JARVIS_HW_TIER_BASELINE in .env.
 * The banner will reappear on the next page refresh until someone manually
 * updates JARVIS_HW_TIER in .env to match the current tier.  This is a
 * known Phase-3 limitation; a future phase should persist the baseline in
 * the DB and update it on dismiss.
 */

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getAISettings, postAISettings, redetectHW, getFirstRunStatus, dismissBanner } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Button } from '@/components/ui/button';
import { errorMessage } from '@/lib/errors';

export function AIPanel() {
  const qc = useQueryClient();

  const { data, isLoading, error: loadError } = useQuery({
    queryKey: QUERY_KEYS.aiSettings.settings(),
    queryFn: getAISettings,
    staleTime: 30_000,
  });

  const { data: setupStatus } = useQuery({
    queryKey: QUERY_KEYS.setup.firstRun(),
    queryFn: getFirstRunStatus,
    staleTime: 60_000,
  });

  const dismissHWBannerMut = useMutation({
    mutationFn: () => dismissBanner('hw_change'),
    onSuccess: () => {
      // Refetch so hw_tier_changed reflects the updated server state.
      // Note: the banner may reappear on next refresh until JARVIS_HW_TIER
      // is updated in .env — see file-level comment for the Phase-3 limitation.
      void qc.invalidateQueries({ queryKey: QUERY_KEYS.setup.firstRun() });
    },
  });

  const redetectMut = useMutation({
    mutationFn: redetectHW,
    onSuccess: (fresh) => {
      qc.setQueryData(QUERY_KEYS.aiSettings.settings(), fresh);
    },
  });

  const applyMut = useMutation({
    mutationFn: postAISettings,
    onSuccess: (fresh) => {
      qc.setQueryData(QUERY_KEYS.aiSettings.settings(), fresh);
    },
  });

  const candidateBackends = new Set((data?.candidates_for_tier ?? []).map((c) => c.backend));
  const recommendedBackend: 'ollama' | 'vllm' =
    data?.recommended_backend === 'vllm' ? 'vllm' : 'ollama';

  // Derive the initial selection from configured state only when it is selectable.
  const rawBackend = data?.configured_backend ?? data?.recommended_backend ?? 'ollama';
  const initialBackend: 'ollama' | 'vllm' =
    rawBackend === 'vllm' && candidateBackends.has('vllm')
      ? 'vllm'
      : rawBackend === 'ollama' && candidateBackends.has('ollama')
        ? 'ollama'
        : recommendedBackend;

  const [selectedBackend, setSelectedBackend] = useState<'ollama' | 'vllm' | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);

  // Resolved values: pending selection falls back to server state
  const activeBackend: 'ollama' | 'vllm' = selectedBackend ?? initialBackend;
  const modelsForBackend = (data?.candidates_for_tier ?? []).filter(
    (c) => c.backend === activeBackend,
  );
  const firstModelForBackend = modelsForBackend[0]?.model ?? '';
  // Reset model when backend changes
  const activeModel =
    selectedModel !== null &&
    modelsForBackend.some((c) => c.model === selectedModel)
      ? selectedModel
      : firstModelForBackend;

  const isDirty =
    activeBackend !== (data?.configured_backend ?? data?.recommended_backend) ||
    activeModel !== (data?.configured_model ?? data?.recommended_model);

  const handleBackendChange = (b: 'ollama' | 'vllm') => {
    setSelectedBackend(b);
    setSelectedModel(null); // reset model on backend switch
  };

  const handleApply = () => {
    applyMut.mutate({ backend: activeBackend, model: activeModel });
  };

  const handleReset = () => {
    setSelectedBackend(null);
    setSelectedModel(null);
    applyMut.reset();
  };

  // Offline banner: observed backend prefix doesn't match configured backend
  const isOffline =
    data?.observed_backend != null &&
    data?.configured_backend != null &&
    !data.observed_backend.startsWith(data.configured_backend);

  // GPU-on-CPU mismatch: GPU was detected at install (baseline is a GPU tier)
  // but the container is now on CPU (overlay not engaged / GPU gone).
  // detect_tier runs INSIDE the paper_ingestion container, so
  // hw_tier_current === "cpu" means the container isn't getting the GPU.
  const gpuCpuMismatch =
    setupStatus?.hw_tier_baseline != null &&
    setupStatus.hw_tier_baseline !== 'cpu' &&
    setupStatus?.hw_tier_current === 'cpu';
  // Suppress the generic hw-change banner when the more specific GPU-on-CPU
  // banner is showing (baseline!==current also makes hw_tier_changed true).
  const showHWChangeBanner = setupStatus?.hw_tier_changed === true && !gpuCpuMismatch;

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading AI settings…</p>;
  }

  if (loadError) {
    return (
      <p className="text-sm text-destructive">
        Failed to load AI settings:{' '}
        {loadError instanceof Error ? loadError.message : 'unknown error'}
      </p>
    );
  }

  return (
    <div className="space-y-6 max-w-2xl">
      {/* Hardware tier row */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-medium">Hardware Tier</h3>
            <p className="text-sm text-muted-foreground mt-0.5">
              Detected tier:{' '}
              <span className="font-mono font-semibold text-foreground">{data?.hw_tier ?? '—'}</span>
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => redetectMut.mutate()}
            disabled={redetectMut.isPending}
          >
            {redetectMut.isPending ? 'Detecting…' : 'Re-detect'}
          </Button>
        </div>
        {data?.eval_report_date && (
          <p className="text-xs text-muted-foreground">
            Eval report: <span className="font-mono">{data.eval_report_date}</span>
          </p>
        )}
      </section>

      {/* GPU-on-CPU mismatch banner — GPU was present at install but the stack is
          now running on CPU (overlay not engaged). No dismiss button: this is a
          config-fix prompt, not a transient notice. */}
      {gpuCpuMismatch && (
        <div
          role="alert"
          data-testid="gpu-cpu-mismatch-banner"
          className="flex items-start justify-between gap-3 rounded-md border border-amber-500 bg-amber-50 dark:bg-amber-950/20 px-4 py-3 text-sm text-amber-900 dark:text-amber-300"
        >
          <span>
            A GPU was detected at install but the stack is running on CPU — your GPU
            isn&apos;t being used. Re-run <code>setup.sh</code> (or set <code>COMPOSE_FILE</code> to
            include <code>docker-compose.gpu.yml</code>) and confirm the NVIDIA container runtime is installed.
          </span>
        </div>
      )}

      {/* HW-change banner — shown when the hardware tier has changed since baseline.
          Amber/orange to distinguish from the yellow offline banner.
          Dismiss writes a system_events row but does NOT update the baseline in .env;
          see file-level comment for the Phase-3 limitation. */}
      {showHWChangeBanner && (
        <div
          role="alert"
          data-testid="hw-change-banner"
          className="flex items-start justify-between gap-3 rounded-md border border-amber-500 bg-amber-50 dark:bg-amber-950/20 px-4 py-3 text-sm text-amber-900 dark:text-amber-300"
        >
          <span>
            Hardware tier has changed
            {setupStatus?.hw_tier_baseline && setupStatus?.hw_tier_current
              ? ` from ${setupStatus.hw_tier_baseline} to ${setupStatus.hw_tier_current}`
              : ''}
            . Review the recommended backend and model below, then click Apply.
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0 text-amber-900 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/30"
            onClick={() => dismissHWBannerMut.mutate()}
            disabled={dismissHWBannerMut.isPending}
          >
            Dismiss
          </Button>
        </div>
      )}

      {/* Offline banner */}
      {isOffline && (
        <div
          role="alert"
          className="rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950/20 px-4 py-3 text-sm text-yellow-800 dark:text-yellow-300"
        >
          Backend may be offline — observed traffic ({data?.observed_backend}) does not match
          configured backend ({data?.configured_backend}). Check service health.
        </div>
      )}

      {(data?.candidate_issues?.length ?? 0) > 0 && (
        <div
          role="alert"
          data-testid="candidate-issues"
          className="rounded-md border border-blue-400 bg-blue-50 dark:bg-blue-950/20 px-4 py-3 text-sm text-blue-900 dark:text-blue-300"
        >
          Some empirical candidates were omitted because they are not in the curated model
          catalog. {data?.candidate_issues[0]}
        </div>
      )}

      {/* Configured vs observed status */}
      <section className="space-y-1">
        <h3 className="text-sm font-medium">Current Status</h3>
        <dl className="text-sm space-y-1">
          <div className="flex gap-2">
            <dt className="w-36 text-muted-foreground">Configured</dt>
            <dd className="font-mono">
              {data?.configured_backend && data?.configured_model
                ? `${data.configured_backend} / ${data.configured_model}`
                : '—'}
            </dd>
          </div>
          <div className="flex gap-2">
            <dt className="w-36 text-muted-foreground">Observed (recent)</dt>
            <dd className="font-mono">
              {data?.observed_backend
                ? `${data.observed_backend} (${Math.round((data?.observed_recent_share ?? 0) * 100)}%)`
                : '—'}
            </dd>
          </div>
          <div className="flex gap-2">
            <dt className="w-36 text-muted-foreground">Recommended</dt>
            <dd className="font-mono">
              {data?.recommended_backend} / {data?.recommended_model}
            </dd>
          </div>
        </dl>
      </section>

      {/* Backend toggle */}
      <section className="space-y-3">
        <h3 className="text-sm font-medium">Backend</h3>
        <div className="flex gap-3">
          {(['vllm', 'ollama'] as const).map((b) => {
            const isRecommended = b === data?.recommended_backend;
            const isActive = activeBackend === b;
            return (
              <button
                key={b}
                type="button"
                onClick={() => handleBackendChange(b)}
                className={[
                  'relative flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'border-primary bg-primary/10 text-primary'
                    : 'border-input bg-background text-muted-foreground hover:border-foreground hover:text-foreground',
                ].join(' ')}
              >
                {b}
                {isRecommended && (
                  <span className="text-xs bg-primary text-primary-foreground rounded px-1.5 py-0.5 leading-none">
                    Recommended
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </section>

      {/* Model dropdown */}
      <section className="space-y-2">
        <h3 className="text-sm font-medium">Model</h3>
        {modelsForBackend.length === 0 ? (
          <p className="text-sm text-muted-foreground">No candidates for this backend and tier.</p>
        ) : (
          <select
            value={activeModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {modelsForBackend.map((c) => (
              <option key={c.model} value={c.model}>
                {c.rank === 1 ? `${c.model} (default)` : c.model}
                {c.score != null ? ` — score ${c.score}` : ''}
              </option>
            ))}
          </select>
        )}

        {/* Reasoning for selected candidate */}
        {(() => {
          const selected = modelsForBackend.find((c) => c.model === activeModel);
          return selected?.reasoning ? (
            <p className="text-xs text-muted-foreground">{selected.reasoning}</p>
          ) : null;
        })()}
      </section>

      {/* Apply / Reset */}
      <div className="flex items-center gap-3 pt-2">
        <Button onClick={handleApply} disabled={applyMut.isPending || !isDirty || !activeModel}>
          {applyMut.isPending ? 'Applying…' : 'Apply'}
        </Button>
        <Button variant="ghost" onClick={handleReset} disabled={!isDirty && !applyMut.isError}>
          Reset
        </Button>

        {applyMut.isSuccess && (
          <p className="text-sm text-green-600 dark:text-green-400">Settings applied.</p>
        )}
      </div>

      {/* Error alert */}
      {applyMut.isError && (
        <div
          role="alert"
          className="rounded-md border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          Failed to apply settings:{' '}
          {errorMessage(applyMut.error, 'unknown error')}
        </div>
      )}
    </div>
  );
}

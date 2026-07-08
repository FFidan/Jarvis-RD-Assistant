/**
 * AIPanel — AI runtime diagnostics panel.
 *
 * Shows hardware tier, configured vs observed backend, candidate evidence, and
 * local runtime guidance. Model assignment is handled by the Quick, Main, and
 * Embedding role cards above this advanced panel.
 *
 * GET  /api/settings/ai           → getAISettings()
 * POST /api/settings/ai/redetect  → redetectHW()
 * GET  /api/setup/status          → getFirstRunStatus() (for hw_tier_changed banner)
 * POST /api/settings/ai/dismiss-banner → dismissBanner('hw_change')
 *
 * HW-change banner limitation (Phase-3): dismissing the banner writes a
 * system_events row but does NOT update JARVIS_HW_TIER_BASELINE in .env.
 * The banner will reappear on the next page refresh until someone manually
 * updates JARVIS_HW_TIER in .env to match the current tier. This is a
 * known Phase-3 limitation; a future phase should persist the baseline in
 * the DB and update it on dismiss.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { dismissBanner, getAISettings, getFirstRunStatus, redetectHW } from '@/lib/api';
import type { AIBackendCandidate } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Button } from '@/components/ui/button';
import { BACKEND_LABELS, BACKEND_TOOLTIP } from '@/lib/labels/backends';

function candidateStatusLabel(candidate: AIBackendCandidate): string | null {
  switch (candidate.evidence) {
    case 'bench':
      return 'Validated';
    case 'pending-bench':
      return 'Needs validation';
    case 'sim-bench':
      return 'Reference run';
    case 'static-benchmark':
    case 'catalog':
      return 'Reference';
    default:
      return null;
  }
}

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
      // is updated in .env; see file-level comment for the Phase-3 limitation.
      void qc.invalidateQueries({ queryKey: QUERY_KEYS.setup.firstRun() });
    },
  });

  const redetectMut = useMutation({
    mutationFn: redetectHW,
    onSuccess: (fresh) => {
      qc.setQueryData(QUERY_KEYS.aiSettings.settings(), fresh);
    },
  });

  const candidates = data?.candidates_for_tier ?? [];
  const recommendedBackend = data?.recommended_backend === 'vllm' ? 'vllm' : 'ollama';

  // Only surface a recommendation the catalog plane can actually assign. The
  // AI model role cards are authoritative, so a recommendation they cannot
  // honour would contradict the user-facing configuration surface.
  const recommendationIsAssignable = candidates.some(
    (candidate) =>
      candidate.backend === data?.recommended_backend && candidate.model === data?.recommended_model,
  );

  const candidateStatusRows = candidates
    .map((candidate) => ({ candidate, label: candidateStatusLabel(candidate) }))
    .filter((row): row is { candidate: AIBackendCandidate; label: string } => row.label !== null);

  const isOffline =
    data?.observed_backend != null &&
    data?.configured_backend != null &&
    !data.observed_backend.startsWith(data.configured_backend);

  // GPU-on-CPU mismatch: GPU was detected at install (baseline is a GPU tier)
  // but the container is now on CPU (overlay not engaged / GPU gone).
  const gpuCpuMismatch =
    setupStatus?.hw_tier_baseline != null &&
    setupStatus.hw_tier_baseline !== 'cpu' &&
    setupStatus?.hw_tier_current === 'cpu';
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
      <section className="space-y-2">
        <div className="flex items-center justify-between gap-3">
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
            Last checked: <span className="font-mono">{data.eval_report_date}</span>
          </p>
        )}
      </section>

      {gpuCpuMismatch && (
        <div
          role="alert"
          data-testid="gpu-cpu-mismatch-banner"
          className="flex items-start justify-between gap-3 rounded-md border border-amber-500 bg-amber-50 dark:bg-amber-950/20 px-4 py-3 text-sm text-amber-900 dark:text-amber-300"
        >
          <span>
            A GPU was detected at install but the stack is running on CPU — your GPU
            isn&apos;t being used. Re-run <code>setup.sh</code> or set <code>COMPOSE_FILE</code>
            to include <code>docker-compose.gpu.yml</code>, then confirm the NVIDIA
            container runtime is installed.{' '}
            <a
              href="https://ffidan.github.io/Jarvis-RD-Assistant/manual/hardware-and-models/"
              target="_blank"
              rel="noopener noreferrer"
              className="underline whitespace-nowrap"
            >
              GPU setup guide →
            </a>
          </span>
        </div>
      )}

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
            . Review the Quick and Main model cards above, then choose models that fit the current hardware.
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
          <details>
            <summary className="cursor-pointer font-medium">
              Some models were excluded from your hardware-tier suggestions
            </summary>
            <p className="mt-2 text-xs opacity-80">
              {data?.candidate_issues?.length} configuration detail
              {data?.candidate_issues?.length === 1 ? '' : 's'} — the recommended model is
              unaffected.
              {(data?.candidate_issues?.length ?? 0) > 0 && (
                <code className="mt-1 block whitespace-normal break-words rounded bg-blue-100 p-1 text-xs dark:bg-blue-900/50">
                  {data?.candidate_issues[0]}
                </code>
              )}
            </p>
          </details>
        </div>
      )}

      <section className="space-y-2 rounded-md border border-input p-3" data-testid="model-assignment-guidance">
        <h3 className="text-sm font-medium">Model routing</h3>
        <p className="text-sm text-muted-foreground">
          Select Quick and Main models in the role cards above. This panel reports runtime
          diagnostics and recommendations only; it does not change active model assignments.
        </p>
      </section>

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
            <dd className="font-mono" data-testid="recommended-value">
              {recommendationIsAssignable
                ? `${data?.recommended_backend} / ${data?.recommended_model}`
                : '—'}
            </dd>
          </div>
        </dl>
      </section>

      <section className="space-y-3" data-testid="runtime-guidance">
        <h3 className="text-sm font-medium">Local runtime guidance</h3>
        <p className="text-xs text-muted-foreground">{BACKEND_TOOLTIP}</p>
        <div className="grid gap-2 sm:grid-cols-2" data-testid="backend-guidance-list">
          {(['ollama', 'vllm'] as const).map((backend) => {
            const isRecommended = backend === recommendedBackend;
            return (
              <div key={backend} className="rounded-md border border-input px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center gap-2 font-medium">
                  {BACKEND_LABELS[backend]}
                  {isRecommended && (
                    <span className="rounded bg-primary px-1.5 py-0.5 text-xs leading-none text-primary-foreground">
                      Recommended
                    </span>
                  )}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  {backend === 'vllm'
                    ? 'Use when you already run vLLM behind the local LiteLLM route, then assign the served model in the role cards above.'
                    : 'Default local runtime for most self-hosted installs; assign installed models in the role cards above.'}
                </p>
              </div>
            );
          })}
        </div>
      </section>

      {candidateStatusRows.length > 0 && (
        <section className="space-y-2" data-testid="candidate-status-list">
          <h3 className="text-sm font-medium">Candidate evidence</h3>
          {candidateStatusRows.map(({ candidate, label }) => (
            <div key={`${candidate.backend}-${candidate.model}`} className="rounded-md border border-input px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-muted-foreground">{candidate.backend} / {candidate.model}</span>
                <span className="rounded border border-input px-1.5 py-0.5 text-muted-foreground">
                  {label}
                </span>
              </div>
              {candidate.reasoning ? (
                <p className="mt-1 text-muted-foreground">{candidate.reasoning}</p>
              ) : null}
            </div>
          ))}
        </section>
      )}
    </div>
  );
}

/**
 * ModelDiagnosticsCard — operator-facing model runtime status.
 *
 * Admin-only (GET /api/settings/ai requires admin). Answers "is my LLM
 * configured and serving correctly?": the detected hardware tier, the backend
 * actually serving recent traffic, and the recommended local model for this
 * hardware. Lives on the System Health page, next to live-service status.
 *
 * Model assignment and hardware-change alerts live on the Settings → Models
 * page, where they are acted on; this is read-only status only.
 *
 * GET  /api/settings/ai          → getAISettings()
 * POST /api/settings/ai/redetect → redetectHW()
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchSystemModels, getAISettings, redetectHW } from '@/lib/api';
import type { SystemModelsResponse } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Button } from '@/components/ui/button';

export function ModelDiagnosticsCard() {
  const qc = useQueryClient();

  const { data, isLoading, error: loadError } = useQuery({
    queryKey: QUERY_KEYS.aiSettings.settings(),
    queryFn: getAISettings,
    staleTime: 30_000,
  });
  const {
    data: systemModels,
    isLoading: routesLoading,
    error: routesError,
  } = useQuery({
    queryKey: QUERY_KEYS.config.systemModels(),
    queryFn: ({ signal }) => fetchSystemModels(signal),
    staleTime: 30_000,
  });

  const redetectMut = useMutation({
    mutationFn: redetectHW,
    onSuccess: (fresh) => {
      qc.setQueryData(QUERY_KEYS.aiSettings.settings(), fresh);
    },
  });

  const recommended =
    data?.recommended_backend && data?.recommended_model
      ? `${data.recommended_backend} / ${data.recommended_model}`
      : '—';

  return (
    <section aria-labelledby="model-diagnostics-heading" data-testid="model-diagnostics">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h2 id="model-diagnostics-heading" className="text-base font-semibold">
            Model runtime
          </h2>
          <p className="text-sm text-muted-foreground mt-1">
            The hardware this instance detected, the backend serving recent requests, and the
            recommended local model. Assign models in Settings → Models.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="shrink-0"
          onClick={() => redetectMut.mutate()}
          disabled={redetectMut.isPending}
        >
          {redetectMut.isPending ? 'Detecting…' : 'Re-detect'}
        </Button>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading model runtime…</p>}

      {loadError && (
        <p className="text-sm text-destructive">
          Couldn&apos;t load model runtime:{' '}
          {loadError instanceof Error ? loadError.message : 'unknown error'}
        </p>
      )}

      {!isLoading && !loadError && (
        <div className="space-y-4">
          <div className="rounded-md border overflow-x-auto">
            <table className="w-full text-sm">
              <tbody>
                <tr className="border-b last:border-0">
                  <th scope="row" className="px-4 py-3 text-left font-medium w-56">
                    Hardware tier
                  </th>
                  <td className="px-4 py-3">
                    <span className="font-mono font-semibold text-foreground">
                      {data?.hw_tier ?? '—'}
                    </span>
                  </td>
                </tr>
                <tr className="border-b last:border-0">
                  <th scope="row" className="px-4 py-3 text-left font-medium">
                    Serving recent traffic
                  </th>
                  <td className="px-4 py-3 font-mono" data-testid="observed-value">
                    {data?.observed_backend
                      ? `${data.observed_backend} (${Math.round((data?.observed_recent_share ?? 0) * 100)}%)`
                      : '—'}
                  </td>
                </tr>
                <tr className="border-b last:border-0">
                  <th scope="row" className="px-4 py-3 text-left font-medium">
                    Recommended for this hardware
                  </th>
                  <td className="px-4 py-3" data-testid="recommended-value">
                    <span className="font-mono">{recommended}</span>
                    {data?.eval_report_date && (
                      <span className="ml-2 text-xs text-muted-foreground">
                        (as of {data.eval_report_date})
                      </span>
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <ActiveRouteTable data={systemModels} isLoading={routesLoading} error={routesError} />

          {(data?.candidate_issues?.length ?? 0) > 0 && (
            <details
              data-testid="candidate-issues"
              className="rounded-md border border-blue-400 bg-blue-50 dark:bg-blue-950/20 px-4 py-3 text-sm text-blue-900 dark:text-blue-300"
            >
              <summary className="cursor-pointer font-medium">
                Some models were excluded from the recommendations for this hardware
              </summary>
              <p className="mt-2 text-xs opacity-80">
                {data?.candidate_issues?.length} configuration detail
                {data?.candidate_issues?.length === 1 ? '' : 's'} — the recommended model is
                unaffected.
                <code className="mt-1 block whitespace-normal break-words rounded bg-blue-100 p-1 text-xs dark:bg-blue-900/50">
                  {data?.candidate_issues[0]}
                </code>
              </p>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

function routeModel(data: SystemModelsResponse, role: 'fast' | 'smart' | 'embed'): string {
  if (role === 'embed') return data.embedding_contract?.model ?? data.current.embed_model ?? 'Not reported';
  return data.current[`${role}_model`] ?? 'Not configured';
}

function routeState(
  data: SystemModelsResponse,
  role: 'fast' | 'smart' | 'embed',
  configured: string,
  serving: string | undefined,
): string {
  if (data.delivery[role] === 'pending_restart') return 'Pending model-service recovery';
  if (!serving) return 'Runtime unavailable';
  return serving === configured ? 'Applied' : 'Configured and serving differ';
}

function ActiveRouteTable({
  data,
  isLoading,
  error,
}: {
  data: SystemModelsResponse | undefined;
  isLoading: boolean;
  error: Error | null;
}) {
  return (
    <section aria-labelledby="active-model-routes-heading" className="space-y-2">
      <div>
        <h3 id="active-model-routes-heading" className="text-sm font-semibold">Active model routes</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Read-only configured and runtime delivery state. Change assignments in Settings - AI models.
        </p>
      </div>
      {isLoading && <p className="text-sm text-muted-foreground">Loading active routes...</p>}
      {error && <p className="text-sm text-destructive">Active route status is unavailable.</p>}
      {data && (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm" data-testid="active-model-routes">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="px-4 py-2 text-left font-medium">Route</th>
                <th className="px-4 py-2 text-left font-medium">Configured</th>
                <th className="px-4 py-2 text-left font-medium">Serving</th>
                <th className="px-4 py-2 text-left font-medium">State</th>
              </tr>
            </thead>
            <tbody>
              {([
                ['fast', 'Quick'],
                ['smart', 'Main'],
                ['embed', 'Embedding'],
              ] as const).map(([role, label]) => {
                const configured = routeModel(data, role);
                const serving = data.routing[role];
                return (
                  <tr key={role} className="border-b last:border-0">
                    <th scope="row" className="px-4 py-2 text-left font-medium">{label}</th>
                    <td className="px-4 py-2 font-mono">{configured}</td>
                    <td className="px-4 py-2 font-mono">{serving ?? 'Not reported'}</td>
                    <td className="px-4 py-2">{routeState(data, role, configured, serving)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/**
 * AIPanel — hardware alerts + a pointer, below the per-role model pickers.
 *
 * Model assignment happens in the role cards above this panel, which already
 * show detected hardware, per-model fit, and a recommended-model prompt. This
 * panel adds only what those pickers don't: the hardware-situation alerts
 * (whose remedy is re-picking models here) and a link to the full runtime
 * status on the admin System Health page. Detailed diagnostics (observed
 * backend, recommended model, candidate notes) live on System Health so they
 * don't crowd the pickers.
 *
 * GET  /api/setup/status               → getFirstRunStatus() (hardware-change state)
 * POST /api/settings/ai/dismiss-banner → dismissBanner('hw_change')
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { dismissBanner, getFirstRunStatus } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Button } from '@/components/ui/button';

export function AIPanel() {
  const qc = useQueryClient();

  const { data: setupStatus } = useQuery({
    queryKey: QUERY_KEYS.setup.firstRun(),
    queryFn: getFirstRunStatus,
    staleTime: 60_000,
  });

  const dismissHWBannerMut = useMutation({
    mutationFn: () => dismissBanner('hw_change'),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QUERY_KEYS.setup.firstRun() });
    },
  });

  // GPU-on-CPU mismatch: a GPU was detected at install (baseline is a GPU tier)
  // but the container is now on CPU (overlay not engaged / GPU gone). This is a
  // more specific diagnosis than a plain tier change, so it takes precedence.
  const gpuCpuMismatch =
    setupStatus?.hw_tier_baseline != null &&
    setupStatus.hw_tier_baseline !== 'cpu' &&
    setupStatus?.hw_tier_current === 'cpu';
  const showHWChangeBanner = setupStatus?.hw_tier_changed === true && !gpuCpuMismatch;

  return (
    <div className="space-y-4" data-testid="model-runtime-note">
      {gpuCpuMismatch && (
        <div
          role="alert"
          data-testid="gpu-cpu-mismatch-banner"
          className="rounded-md border border-amber-500 bg-amber-50 dark:bg-amber-950/20 px-4 py-3 text-sm text-amber-900 dark:text-amber-300"
        >
          A GPU was detected at install but the stack is running on CPU — your GPU isn&apos;t
          being used, so the models above are running slowly on CPU. Re-run <code>setup.sh</code>{' '}
          or set <code>COMPOSE_FILE</code> to include <code>docker-compose.gpu.yml</code>, then
          confirm the NVIDIA container runtime is installed.{' '}
          <a
            href="https://limitcycle-oss.github.io/Jarvis-RD-Assistant/manual/hardware-and-models/"
            target="_blank"
            rel="noopener noreferrer"
            className="underline whitespace-nowrap"
          >
            GPU setup guide →
          </a>
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
            . Review the model cards above and pick models that fit the current hardware.
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

      <p className="text-sm text-muted-foreground">
        Detected hardware, the backend serving recent traffic, and the recommended model for this
        machine are on{' '}
        <Link to="/admin/system-health" className="underline hover:text-foreground">
          System Health
        </Link>
        .
      </p>
    </div>
  );
}

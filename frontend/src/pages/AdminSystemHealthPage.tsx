/**
 * Admin system-readiness viewer.
 *
 * Accessible at /admin/system-health. Requires admin role; non-admins are
 * redirected by the AdminOnlyRoute guard in App.tsx.
 *
 * Shows overall readiness status (green/amber/red) and a per-check breakdown
 * from GET /api/system/readiness, plus a live services section showing
 * real-time status for every stack component via fetchStackHealth().
 *
 * Each check row includes an info tooltip with a human-readable explanation of
 * what the check measures and what to do if it's red. A context banner is shown
 * when the instance is in development mode (any dev_* flag is red, or
 * environment is not "production").
 */

import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  getSystemReadiness,
  fetchStackHealth,
  type ReadinessCheck,
  type ServiceHealth,
  type ServiceHealthStatus,
} from '@/lib/api';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { ModelDiagnosticsCard } from '@/components/admin/ModelDiagnosticsCard';
import { StorageCard } from '@/components/admin/StorageCard';

type StatusLevel = ReadinessCheck['status'];

const STATUS_CLASSES: Record<StatusLevel, string> = {
  green: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  amber: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
  red: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
};

const SERVICE_STATUS_CLASSES: Record<ServiceHealthStatus, string> = {
  ok: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  degraded: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
  down: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
  unknown: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
};

/**
 * Human-readable labels for each backend check name (snake_case → plain text).
 * Must stay in sync with CHECK_EXPLANATIONS below.
 */
const DISPLAY_LABELS: Record<string, string> = {
  dev_auth_bypass: 'Auth bypass (dev)',
  dev_error_detail: 'Error detail (dev)',
  dev_cors_open: 'Open CORS (dev)',
  dev_smtp_log_only: 'SMTP log-only (dev)',
  dev_crypto_relaxed: 'Relaxed crypto (dev)',
  environment: 'Environment',
  api_key: 'API key',
  smtp: 'Email delivery (SMTP)',
  https: 'HTTPS / TLS',
  audit_log: 'Audit log',
  owner_identity: 'Instance owner',
};

/**
 * Static per-check explanations keyed by the exact backend check name.
 * Values describe: what the check measures, why it may be red/amber, and
 * what to do before a public production deployment.
 */
const CHECK_EXPLANATIONS: Record<string, string> = {
  dev_auth_bypass:
    'Security bypass that allows unrestricted sign-in.',
  dev_error_detail:
    'Full error tracebacks exposed to API clients.',
  dev_cors_open:
    'Cross-origin API access unrestricted.',
  dev_smtp_log_only:
    'Email delivery suppressed; logs contain only non-secret status metadata.',
  dev_crypto_relaxed:
    'Weakened session token security.',
  environment:
    'Deployment environment setting.',
  api_key:
    'Primary access control credential.',
  smtp:
    'Email delivery configuration.',
  https:
    'Transport-layer encryption.',
  audit_log:
    'Security event logging.',
  owner_identity:
    'Account allowed to recover and transfer ownership of this JARVIS instance.',
};

const STATUS_VERDICT: Record<StatusLevel, string> = {
  green: 'Ready',
  amber: 'Review needed',
  red: 'Action required',
};

function StatusBadge({ status }: { status: StatusLevel }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_CLASSES[status]}`}
    >
      {STATUS_VERDICT[status]}
    </span>
  );
}

function ServiceStatusBadge({ status }: { status: ServiceHealthStatus }) {
  const labels: Record<ServiceHealthStatus, string> = {
    ok: 'Running',
    degraded: 'Degraded',
    down: 'Down',
    unknown: 'Unknown',
  };
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${SERVICE_STATUS_CLASSES[status]}`}
      data-testid={`svc-status-badge-${status}`}
    >
      {labels[status]}
    </span>
  );
}

const SERVICE_DISPLAY_LABELS: Record<string, string> = {
  qdrant: 'Search index (Qdrant)',
  litellm: 'AI model router (LiteLLM)',
  vector: 'Log collector (optional)',
};

/** Display label for a service — maps technical names to self-hoster-friendly labels. */
function serviceDisplayLabel(svc: ServiceHealth): string {
  return SERVICE_DISPLAY_LABELS[svc.name] ?? svc.label;
}

const SERVICE_CONSEQUENCE: Record<string, string> = {
  qdrant: 'Semantic search and citation graph are unavailable.',
  litellm: 'AI-powered features (Ask, Pulse, summaries) are unavailable.',
  ollama: 'Local model inference is unavailable.',
  postgres: 'The database is unreachable — the app cannot function.',
  paper_ingestion: 'New papers cannot be ingested.',
  learning_engine: 'Learning-card generation is unavailable.',
};

/** Plain-language note shown in the detail column for a service. */
function serviceDetailNote(svc: ServiceHealth): string | null {
  if (svc.name === 'vector' && svc.status === 'unknown') {
    return 'Optional log shipper — not running; this is normal unless you enabled the observability profile.';
  }
  if (svc.status === 'down' || svc.status === 'degraded') {
    return SERVICE_CONSEQUENCE[svc.name] ?? null;
  }
  return null;
}

/** Returns true when the instance appears to be running in development mode. */
function isDevMode(checks: ReadinessCheck[]): boolean {
  return checks.some(
    (c) =>
      (c.name.startsWith('dev_') && c.status === 'red') ||
      (c.name === 'environment' && c.status !== 'green'),
  );
}

export function AdminSystemHealthPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.admin.systemHealth(),
    queryFn: getSystemReadiness,
  });

  const {
    data: stackData,
    isLoading: stackLoading,
    isError: stackError,
  } = useQuery({
    queryKey: QUERY_KEYS.stack.health(),
    queryFn: fetchStackHealth,
    refetchInterval: 30_000,
    retry: false,
  });

  const showDevBanner = data ? isDevMode(data.checks) : false;

  return (
    <div className="p-6 space-y-6">
      <div>
        <AdminBreadcrumb page="System health" />
        <h1 className="text-2xl font-semibold">System health</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Deployment readiness checks and live service status for all components.
        </p>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Live services section                                               */}
      {/* ------------------------------------------------------------------ */}
      <section aria-labelledby="live-services-heading" data-testid="live-services-section">
        <h2 id="live-services-heading" className="text-base font-semibold mb-3">
          Live services
        </h2>
        <p className="text-sm text-muted-foreground mb-3">
          Current status of every component in your stack, refreshed every 30 seconds.
        </p>

        {stackLoading && (
          <div className="text-sm text-muted-foreground">Checking services…</div>
        )}
        {stackError && (
          <div className="text-sm text-destructive">Could not reach the health endpoints.</div>
        )}

        {!stackLoading && !stackError && stackData && (
          <>
          {stackData.overall === 'unknown' ? (
            <p className="text-sm text-muted-foreground mb-3" data-testid="stack-summary">
              Could not determine service status — the health endpoints did not respond in time.
            </p>
          ) : (stackData.downCount > 0 || stackData.degradedCount > 0) ? (
            <p className="text-sm mb-3" data-testid="stack-summary">
              {stackData.downCount > 0 && (
                <span className="text-red-600 dark:text-red-400 font-medium">
                  {stackData.downCount} service{stackData.downCount !== 1 ? 's' : ''} down
                  {stackData.degradedCount > 0 ? ', ' : '.'}
                </span>
              )}
              {stackData.degradedCount > 0 && (
                <span className="text-yellow-600 dark:text-yellow-400 font-medium">
                  {stackData.degradedCount} degraded.
                </span>
              )}
            </p>
          ) : (
            <p className="text-sm text-green-600 dark:text-green-400 mb-3" data-testid="stack-summary">
              All services running.
            </p>
          )}
          <div className="rounded-md border overflow-x-auto" data-testid="live-services-table">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/50">
                  <th className="px-4 py-3 text-left font-medium">Service</th>
                  <th className="px-4 py-3 text-left font-medium">Status</th>
                  <th className="px-4 py-3 text-left font-medium">Note</th>
                </tr>
              </thead>
              <tbody>
                {stackData.services.map((svc) => {
                  const note = serviceDetailNote(svc);
                  return (
                    <tr key={svc.name} className="border-b last:border-0" data-testid={`live-svc-row-${svc.name}`}>
                      <td className="px-4 py-3 font-medium">{serviceDisplayLabel(svc)}</td>
                      <td className="px-4 py-3">
                        <ServiceStatusBadge status={svc.status} />
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {note ?? '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          </>
        )}
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Disk usage                                                          */}
      {/* ------------------------------------------------------------------ */}
      <StorageCard />

      {/* ------------------------------------------------------------------ */}
      {/* Model runtime diagnostics                                           */}
      {/* ------------------------------------------------------------------ */}
      <ModelDiagnosticsCard />

      {/* ------------------------------------------------------------------ */}
      {/* Readiness checks section                                            */}
      {/* ------------------------------------------------------------------ */}
      <section aria-labelledby="readiness-heading">
        <h2 id="readiness-heading" className="text-base font-semibold mb-3">
          Pre-deployment checklist
        </h2>
        <p className="text-sm text-muted-foreground mb-3">
          Settings that must be reviewed before sharing this instance with other people.
        </p>

        {isLoading && (
          <div className="text-sm text-muted-foreground">Loading readiness checks…</div>
        )}
        {isError && (
          <div className="text-sm text-destructive">Failed to load system health.</div>
        )}

        {!isLoading && !isError && data && (
          <div className="space-y-4">
            {showDevBanner && (
              <div
                role="alert"
                className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-300"
              >
                This instance is running in development mode. The red checks below are expected
                for local development — they flag settings that must be changed before a public
                production deployment. See the{' '}
                <a
                  href="https://limitcycle-oss.github.io/jarvis-rd-assistant/DEPLOYMENT/#production-readiness-check"
                  className="underline"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Deployment Guide → Production Readiness Check
                </a>
                .
              </div>
            )}

            <div className="flex items-center gap-3">
              <span className="text-sm font-medium text-muted-foreground">Overall status</span>
              <StatusBadge status={data.status} />
            </div>

            <div className="rounded-md border overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="px-4 py-3 text-left font-medium">Check</th>
                    <th className="px-4 py-3 text-left font-medium">Status</th>
                    <th className="px-4 py-3 text-left font-medium">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {data.checks.map((check) => (
                    <tr key={check.name} className="border-b last:border-0">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <span className="font-medium">
                            {DISPLAY_LABELS[check.name] ?? check.name}
                          </span>
                          {CHECK_EXPLANATIONS[check.name] && (
                            <InfoTooltip
                              content={CHECK_EXPLANATIONS[check.name]}
                              side="right"
                            />
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={check.status} />
                      </td>
                      <td className="px-4 py-3 text-muted-foreground break-all">
                        <div className="space-y-2">
                          {check.detail && (
                            <div className="text-xs">{check.detail}</div>
                          )}
                          {check.remediation && (
                            <div className="text-xs text-orange-600 dark:text-orange-400 font-medium">
                              {check.remediation}
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {data.checks.length === 0 && (
                    <tr>
                      <td
                        colSpan={3}
                        className="px-4 py-8 text-center text-muted-foreground"
                      >
                        No readiness checks reported.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

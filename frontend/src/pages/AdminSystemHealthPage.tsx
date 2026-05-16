/**
 * Admin system-readiness viewer (WS-PRE-PUBLIC-CHECKLIST).
 *
 * Accessible at /admin/system-health. Requires admin role; non-admins are
 * redirected by the AdminOnlyRoute guard in App.tsx.
 *
 * Shows overall readiness status (green/amber/red) and a per-check breakdown
 * from GET /api/system/readiness.
 *
 * Each check row includes an info tooltip with a human-readable explanation of
 * what the check measures and what to do if it's red. A context banner is shown
 * when the instance is in development mode (any dev_* flag is red, or
 * environment is not "production").
 */

import { useQuery } from '@tanstack/react-query';
import { getSystemReadiness, type ReadinessCheck } from '@/lib/api';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';
import { InfoTooltip } from '@/components/ui/info-tooltip';

type StatusLevel = ReadinessCheck['status'];

const STATUS_CLASSES: Record<StatusLevel, string> = {
  green: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  amber: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
  red: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
};

/**
 * Static per-check explanations keyed by the exact backend check name.
 * Values describe: what the check measures, why it may be red/amber, and
 * what to do before a public production deployment.
 */
const CHECK_EXPLANATIONS: Record<string, string> = {
  dev_auth_bypass:
    'Development auth bypass is ON. Expected red on a local dev box; set DEV_AUTH_BYPASS=false (and DEV_MODE=false) for a production deployment.',
  dev_error_detail:
    'Full error tracebacks are being sent to API clients. Acceptable locally; set DEV_ERROR_DETAIL=false in production so stack traces are never leaked to end users.',
  dev_cors_open:
    'CORS is configured to accept requests from any origin. Fine for local development; set DEV_CORS_OPEN=false and restrict CORS_ORIGINS to your domain before going live.',
  dev_smtp_log_only:
    'Magic-link emails are printed to the server log instead of delivered. Set DEV_SMTP_LOG_ONLY=false and configure real SMTP credentials for production.',
  dev_crypto_relaxed:
    'Relaxed cryptography settings are active (e.g. weaker token signing). Set DEV_CRYPTO_RELAXED=false in production to enforce full security parameters.',
  environment:
    'The deployment environment is not set to "production". Set ENVIRONMENT=production before going live — some safeguards only activate in production mode.',
  api_key:
    'The JARVIS API key controls access to the whole application. It must be present and at least 32 characters long; generate one with openssl rand -hex 32 and set JARVIS_API_KEY.',
  smtp:
    'No SMTP configured — magic-link sign-in emails print to the server log instead of being delivered. Fine for local use; configure SMTP_HOST (and related vars) before inviting real users.',
  https:
    'Served over plain HTTP. Fine locally; production must terminate TLS (the bundled Caddy/nginx does this automatically when pointed at a real domain).',
  audit_log:
    'Tracks security-relevant events (logins, admin actions). Green means the audit_log table is reachable and contains rows; amber means the table could not be queried.',
};

function StatusBadge({ status }: { status: StatusLevel }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_CLASSES[status]}`}
    >
      {status}
    </span>
  );
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
    queryKey: ['admin', 'system-health'],
    queryFn: getSystemReadiness,
  });

  const showDevBanner = data ? isDevMode(data.checks) : false;

  return (
    <div className="p-6 space-y-6">
      <div>
        <AdminBreadcrumb page="System health" />
        <h1 className="text-2xl font-semibold">System health</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Pre-deployment readiness checks for all services.
        </p>
      </div>

      {isLoading && (
        <div className="text-sm text-muted-foreground">Loading system health…</div>
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
              production deployment. See the User Guide → Production Checklist.
            </div>
          )}

          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-muted-foreground">Overall status</span>
            <StatusBadge status={data.status} />
          </div>

          <div className="rounded-md border">
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
                    <td className="px-4 py-3 font-medium">
                      <span className="inline-flex items-center gap-1.5">
                        {check.name}
                        {CHECK_EXPLANATIONS[check.name] && (
                          <InfoTooltip
                            content={CHECK_EXPLANATIONS[check.name]}
                            side="right"
                          />
                        )}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={check.status} />
                    </td>
                    <td className="px-4 py-3 text-muted-foreground break-all">
                      {check.detail || '—'}
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
    </div>
  );
}

/**
 * Admin system-readiness viewer (WS-PRE-PUBLIC-CHECKLIST).
 *
 * Accessible at /admin/system-health. Requires admin role; non-admins are
 * redirected by the AdminOnlyRoute guard in App.tsx.
 *
 * Shows overall readiness status (green/amber/red) and a per-check breakdown
 * from GET /api/system/readiness.
 */

import { useQuery } from '@tanstack/react-query';
import { getSystemReadiness, type ReadinessCheck } from '@/lib/api';

type StatusLevel = ReadinessCheck['status'];

const STATUS_CLASSES: Record<StatusLevel, string> = {
  green: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  amber: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
  red: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
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

export function AdminSystemHealthPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['admin', 'system-health'],
    queryFn: getSystemReadiness,
  });

  return (
    <div className="p-6 space-y-6">
      <div>
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
                    <td className="px-4 py-3 font-medium">{check.name}</td>
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

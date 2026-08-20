/**
 * Admin audit-log viewer.
 *
 * Accessible at /admin/audit-log. Requires admin role; non-admins are
 * redirected by the AdminOnlyRoute guard in App.tsx.
 *
 * Features:
 * - Newest-first table of audit_log rows.
 * - Action-prefix filter input (server-side LIKE prefix||'%').
 * - "Load more" cursor pagination via before_id.
 */

import { useState } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { formatRelativeTime } from '@/lib/relative-time';
import { listAuditLog, type AuditLogEntry } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';

function formatMetadata(meta: Record<string, unknown> | null): string {
  if (!meta || Object.keys(meta).length === 0) return '—';
  try {
    return JSON.stringify(meta);
  } catch {
    return '—';
  }
}

const ACTION_LABELS: Readonly<Record<string, string>> = {
  'llm.route.change': 'Model route changed',
  'secret.rotate': 'Secret replaced',
  'secret.remove': 'Secret removed',
};

function actionLabel(action: string): string | undefined {
  return ACTION_LABELS[action];
}

export function AdminAuditLogPage() {
  const [filterInput, setFilterInput] = useState('');
  const [actionPrefix, setActionPrefix] = useState('');

  const {
    data,
    isLoading,
    isError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: QUERY_KEYS.admin.auditLog(actionPrefix),
    queryFn: ({ pageParam }) =>
      listAuditLog({
        limit: 50,
        beforeId: pageParam,
        actionPrefix: actionPrefix || undefined,
      }),
    initialPageParam: null as number | null,
    getNextPageParam: (lastPage) => lastPage.next_before_id ?? undefined,
  });

  function applyFilter(e: React.FormEvent) {
    e.preventDefault();
    setActionPrefix(filterInput.trim());
  }

  const entries: AuditLogEntry[] = data?.pages.flatMap((p) => p.entries) ?? [];

  return (
    <div className="p-6 space-y-6">
      <div>
        <AdminBreadcrumb page="Audit log" />
        <h1 className="text-2xl font-semibold">Audit log</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Security and administrative events, newest first.
        </p>
      </div>

      <form onSubmit={applyFilter} className="flex flex-col sm:flex-row items-end gap-3">
        <div className="space-y-2">
          <Label htmlFor="action-prefix">Filter by action prefix</Label>
          <Input
            id="action-prefix"
            placeholder="auth.magic_link"
            value={filterInput}
            onChange={(e) => setFilterInput(e.target.value)}
            className="w-full sm:w-72"
          />
        </div>
        <Button type="submit" variant="outline">
          Apply
        </Button>
      </form>

      {isLoading && (
        <div className="text-sm text-muted-foreground">Loading audit log…</div>
      )}
      {isError && (
        <div className="text-sm text-destructive">Failed to load audit log.</div>
      )}

      {!isLoading && !isError && (
        <div className="rounded-md border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="px-4 py-3 text-left font-medium">When</th>
                <th className="px-4 py-3 text-left font-medium">Action</th>
                <th className="px-4 py-3 text-left font-medium">User</th>
                <th className="px-4 py-3 text-left font-medium">Resource</th>
                <th className="px-4 py-3 text-left font-medium">Metadata</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id} className="border-b last:border-0">
                  <td className="px-4 py-3 text-muted-foreground whitespace-nowrap">
                    {formatRelativeTime(entry.created_at)}
                  </td>
                  <td className="px-4 py-3 font-medium">
                    {actionLabel(entry.action) ? (
                      <span className="space-y-0.5">
                        <span className="block">{actionLabel(entry.action)}</span>
                        <code className="block text-xs font-normal text-muted-foreground">
                          {entry.action}
                        </code>
                      </span>
                    ) : (
                      entry.action
                    )}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {entry.user_id ?? '—'}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground break-all">
                    {entry.resource}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground break-all">
                    {formatMetadata(entry.metadata)}
                  </td>
                </tr>
              ))}
              {entries.length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-8 text-center text-muted-foreground"
                  >
                    No audit events recorded.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
          >
            {isFetchingNextPage ? 'Loading…' : 'Load more'}
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * Admin audit-log viewer (WS-ADMIN-AUDIT).
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
import { formatDistanceToNow } from 'date-fns';
import { listAuditLog, type AuditLogEntry } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';

function formatDate(iso: string): string {
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

function formatMetadata(meta: Record<string, unknown> | null): string {
  if (!meta || Object.keys(meta).length === 0) return '—';
  try {
    return JSON.stringify(meta);
  } catch {
    return '—';
  }
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
    queryKey: ['admin', 'audit-log', actionPrefix],
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

      <form onSubmit={applyFilter} className="flex items-end gap-3">
        <div className="space-y-2">
          <Label htmlFor="action-prefix">Filter by action prefix</Label>
          <Input
            id="action-prefix"
            placeholder="auth.magic_link"
            value={filterInput}
            onChange={(e) => setFilterInput(e.target.value)}
            className="w-72"
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
        <div className="rounded-md border">
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
                    {formatDate(entry.created_at)}
                  </td>
                  <td className="px-4 py-3 font-medium">{entry.action}</td>
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

/**
 * Admin Backup panel.
 *
 * Accessible at /admin/backups (admin role; AdminOnlyRoute guards the route).
 * Shows sidecar status, restore points grouped from GET /api/admin/backups/restore-points,
 * allows an on-demand backup (confirm), per-file download (expandable per card),
 * and a read-only restore runbook (no in-app restore execution).
 */

import { useState } from 'react';
import {
  useQuery,
  useMutation,
  useQueryClient,
  keepPreviousData,
} from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  getBackupStatus,
  getRestorePoints,
  triggerBackup,
  downloadBackup,
  type RestorePoint,
} from '@/lib/api/backups';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';
import { RestoreRunbook } from '@/components/admin/RestoreRunbook';

const STORE_LABELS: Record<string, string> = {
  jarvis: 'Main database',
  litellm: 'AI model router database',
  secrets: 'Secrets',
  qdrant: 'Search index (Qdrant)',
};

function storeLabel(store: string): string {
  return STORE_LABELS[store] ?? store;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatAge(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function RestorePointCard({
  point,
  retentionDays,
  onDownload,
}: {
  point: RestorePoint;
  retentionDays: number | null;
  onDownload: (name: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const storeBadges = [
    ...point.stores.map(storeLabel),
    ...point.qdrant_collections.map((c) => `Qdrant: ${c}`),
  ];

  return (
    <div className="rounded-md border p-4 space-y-3" data-testid="restore-point-card">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="text-sm font-medium">{formatAge(point.created_at)}</div>
          <div className="text-xs text-muted-foreground">
            {new Date(point.created_at).toLocaleString()} · {formatBytes(point.total_size_bytes)}
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {point.complete ? (
            <span className="inline-flex rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-400">
              Complete
            </span>
          ) : (
            <span className="inline-flex rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">
              Incomplete
            </span>
          )}
          {point.encrypted ? (
            <span className="inline-flex rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-400">
              Encrypted
            </span>
          ) : (
            <span className="inline-flex rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">
              Not encrypted
            </span>
          )}
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {storeBadges.map((label) => (
          <span
            key={label}
            className="inline-flex rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground"
          >
            {label}
          </span>
        ))}
      </div>

      {retentionDays != null && (
        <div className="text-xs text-muted-foreground">Kept for {retentionDays} days</div>
      )}

      <div>
        <button
          type="button"
          className="text-xs font-medium text-primary"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Hide details' : 'Details'} ({point.files.length} file
          {point.files.length !== 1 ? 's' : ''})
        </button>
        {expanded && (
          <table className="mt-2 w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-xs">
                <th className="px-3 py-2 text-left font-medium">File</th>
                <th className="px-3 py-2 text-left font-medium">Component</th>
                <th className="px-3 py-2 text-left font-medium">Size</th>
                <th className="px-3 py-2 text-right font-medium">Download</th>
              </tr>
            </thead>
            <tbody>
              {point.files.map((f) => (
                <tr key={f.filename} className="border-b last:border-0">
                  <td className="px-3 py-2 font-mono text-xs break-all">{f.filename}</td>
                  <td className="px-3 py-2">{storeLabel(f.store)}</td>
                  <td className="px-3 py-2">{formatBytes(f.size_bytes)}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      className="rounded-md border px-3 py-1 text-xs font-medium"
                      onClick={() => onDownload(f.filename)}
                    >
                      Download
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export function AdminBackupsPage() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  const {
    data: status,
    isLoading: statusLoading,
    isError: statusError,
  } = useQuery({
    queryKey: ['admin', 'backups', 'status'],
    queryFn: getBackupStatus,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });

  const {
    data: restore,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ['admin', 'restore-points'],
    queryFn: getRestorePoints,
    placeholderData: keepPreviousData,
  });

  const trigger = useMutation({
    mutationFn: triggerBackup,
    onSuccess: () => {
      toast.success('Backup requested. The backup runs in the background.');
      setConfirming(false);
      void queryClient.invalidateQueries({ queryKey: ['admin', 'restore-points'] });
    },
    onError: (e: unknown) => {
      toast.error(e instanceof Error ? e.message : 'Could not request a backup.');
      setConfirming(false);
    },
  });

  const handleDownload = async (name: string) => {
    try {
      await downloadBackup(name);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Download failed.');
    }
  };

  const points = restore?.restore_points ?? [];

  return (
    <div className="p-6 space-y-6">
      <div>
        <AdminBreadcrumb page="Backups" />
        <h1 className="text-2xl font-semibold">Backups</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Disaster-recovery archives (databases, vectors, and secrets). Archives contain platform
          secrets — keep downloads secure.
        </p>
      </div>

      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="text-sm text-muted-foreground" data-testid="backup-status">
          {statusLoading && !status ? (
            'Checking backup status…'
          ) : statusError && !status ? (
            'Backup status unavailable.'
          ) : status && !status.backup_dir_available ? (
            'Backup storage is not available.'
          ) : status?.last_run_succeeded === false ? (
            <span className="text-amber-700 dark:text-amber-400">
              Last backup attempt failed — check the backup service.
              {status.last_attempt_at ? ` (${formatAge(status.last_attempt_at)})` : ''}
            </span>
          ) : status?.last_run_at ? (
            `Last backup ${formatAge(status.last_run_at)} · ${status.archive_count} archive${status.archive_count !== 1 ? 's' : ''}`
          ) : (
            'No backups yet.'
          )}
        </div>
        {confirming ? (
          <div className="flex items-center gap-2">
            <span className="text-sm">Run a backup now?</span>
            <button
              type="button"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
              onClick={() => trigger.mutate()}
              disabled={trigger.isPending}
            >
              Confirm
            </button>
            <button
              type="button"
              className="rounded-md border px-3 py-1.5 text-sm"
              onClick={() => setConfirming(false)}
              disabled={trigger.isPending}
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="rounded-md border px-3 py-1.5 text-sm font-medium"
            onClick={() => setConfirming(true)}
          >
            Run backup now
          </button>
        )}
      </div>

      {isLoading && <div className="text-sm text-muted-foreground">Loading backups…</div>}
      {isError && <div className="text-sm text-destructive">Failed to load backups.</div>}

      {!isLoading && !isError && (
        <div className="space-y-3">
          {points.length === 0 ? (
            <div className="rounded-md border px-4 py-8 text-center text-sm text-muted-foreground">
              No restore points found.
            </div>
          ) : (
            points.map((point) => (
              <RestorePointCard
                key={point.timestamp}
                point={point}
                retentionDays={restore?.retention_days ?? null}
                onDownload={(name) => void handleDownload(name)}
              />
            ))
          )}
        </div>
      )}

      <RestoreRunbook />
    </div>
  );
}

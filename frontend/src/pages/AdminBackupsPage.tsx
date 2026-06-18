/**
 * Admin Backup panel.
 *
 * Accessible at /admin/backups (admin role; AdminOnlyRoute guards the route).
 * Lists DR archives from GET /api/admin/backups, shows sidecar status, allows
 * an on-demand backup (confirm), per-row download, and a read-only restore
 * runbook (no in-app restore execution).
 */

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  listBackups,
  getBackupStatus,
  triggerBackup,
  downloadBackup,
  type BackupEntry,
} from '@/lib/api/backups';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';
import { RestoreRunbook } from '@/components/admin/RestoreRunbook';

const STORE_LABELS: Record<BackupEntry['store'], string> = {
  jarvis: 'Main database',
  litellm: 'Model router DB',
  secrets: 'Secrets',
  qdrant: 'Vectors (Qdrant)',
};

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

export function AdminBackupsPage() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  const { data: entries, isLoading, isError } = useQuery({
    queryKey: ['admin', 'backups'],
    queryFn: listBackups,
  });

  const { data: status } = useQuery({
    queryKey: ['admin', 'backups', 'status'],
    queryFn: getBackupStatus,
    refetchInterval: 30_000,
  });

  const trigger = useMutation({
    mutationFn: triggerBackup,
    onSuccess: () => {
      toast.success('Backup requested. The backup runs in the background.');
      setConfirming(false);
      void queryClient.invalidateQueries({ queryKey: ['admin', 'backups'] });
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
          {status?.backup_dir_available
            ? status.last_run_at
              ? `Last backup ${formatAge(status.last_run_at)} · ${status.archive_count} archive${status.archive_count !== 1 ? 's' : ''}`
              : 'No backups yet.'
            : 'Backup sidecar not running — enable the backup profile to produce archives.'}
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

      {!isLoading && !isError && entries && (
        <div className="rounded-md border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="px-4 py-3 text-left font-medium">Archive</th>
                <th className="px-4 py-3 text-left font-medium">Store</th>
                <th className="px-4 py-3 text-left font-medium">Size</th>
                <th className="px-4 py-3 text-left font-medium">Age</th>
                <th className="px-4 py-3 text-left font-medium">Encrypted</th>
                <th className="px-4 py-3 text-right font-medium">Download</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.filename} className="border-b last:border-0">
                  <td className="px-4 py-3 font-mono text-xs break-all">{e.filename}</td>
                  <td className="px-4 py-3">{STORE_LABELS[e.store] ?? e.store}</td>
                  <td className="px-4 py-3">{formatBytes(e.size_bytes)}</td>
                  <td className="px-4 py-3 text-muted-foreground">{formatAge(e.modified_at)}</td>
                  <td className="px-4 py-3">
                    {e.encrypted ? (
                      <span className="inline-flex rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-400">
                        Encrypted
                      </span>
                    ) : (
                      <span className="inline-flex rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">
                        Plaintext
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      className="rounded-md border px-3 py-1 text-xs font-medium"
                      onClick={() => void handleDownload(e.filename)}
                    >
                      Download
                    </button>
                  </td>
                </tr>
              ))}
              {entries.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">
                    No backup archives found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <RestoreRunbook />
    </div>
  );
}

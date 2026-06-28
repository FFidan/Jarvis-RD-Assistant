/**
 * Admin Backup panel.
 *
 * Accessible at /admin/backups (admin role; AdminOnlyRoute guards the route).
 * Shows sidecar status, restore points grouped from GET /api/admin/backups/restore-points,
 * allows an on-demand backup (confirm), per-file download (expandable per card),
 * a guided one-click restore (typed-RESTORE confirm + polled progress that degrades
 * gracefully while the app is briefly unreachable mid-restore), and the manual host
 * runbook as the advanced fallback.
 */

import { useEffect, useState } from 'react';
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
  requestRestore,
  getRestoreStatus,
  type RestorePoint,
} from '@/lib/api/backups';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';
import { RestoreRunbook } from '@/components/admin/RestoreRunbook';
import { TypedConfirmDialog } from '@/components/admin/TypedConfirmDialog';

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
  onRestore,
  restoringTimestamp,
}: {
  point: RestorePoint;
  retentionDays: number | null;
  onDownload: (name: string) => void;
  onRestore: (timestamp: string) => void;
  restoringTimestamp: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const isNewer = point.compat === 'newer';
  const isThisRestoring = restoringTimestamp === point.timestamp;
  const restoreDisabled = isNewer || !point.complete || restoringTimestamp !== null;
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

      <div className="flex flex-col gap-1.5">
        <button
          type="button"
          className="self-start rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
          disabled={restoreDisabled}
          onClick={() => onRestore(point.timestamp)}
        >
          {isThisRestoring ? 'Restoring…' : 'Restore to this point'}
        </button>
        {isNewer && (
          <span className="text-xs text-muted-foreground">
            This backup is newer than the current app version — update first.
          </span>
        )}
      </div>
    </div>
  );
}

export function AdminBackupsPage() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [confirmTs, setConfirmTs] = useState<string | null>(null);
  const [restoringTimestamp, setRestoringTimestamp] = useState<string | null>(null);

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

  // Poll restore progress only while a restore is being tracked. A numeric
  // refetchInterval keeps polling even when the query errors, so the brief
  // app-down window mid-restore degrades (below) instead of dropping out.
  const restoreStatus = useQuery({
    queryKey: ['admin', 'restore-status'],
    queryFn: getRestoreStatus,
    enabled: restoringTimestamp !== null,
    refetchInterval: restoringTimestamp ? 3000 : false,
    retry: false,
  });

  const restoreState = restoreStatus.data?.state;
  useEffect(() => {
    if (!restoringTimestamp) return;
    // Only a terminal state stops tracking. 'pending' (queued — the sidecar
    // polls every few seconds before it writes the first status), 'running', and
    // a transient 'idle'/undefined all keep the poll alive. The backend reports
    // 'pending' whenever the request sentinel exists, so a freshly-requested
    // restore never reads as 'idle' and a leftover status file from a prior run
    // can never end tracking early.
    if (restoreState === 'done') {
      toast.success('Restore complete. Your data has been restored.');
      void queryClient.invalidateQueries({ queryKey: ['admin', 'restore-points'] });
      setRestoringTimestamp(null);
    } else if (restoreState === 'failed') {
      setRestoringTimestamp(null);
    }
  }, [restoreState, restoringTimestamp, queryClient]);

  const restoreMutation = useMutation({
    mutationFn: (timestamp: string) => requestRestore(timestamp, 'RESTORE'),
    onSuccess: (_data, timestamp) => {
      // Evict any terminal state (e.g. 'done') left by a previous restore so the
      // new restore starts from a clean fetch rather than the stale cached state.
      queryClient.removeQueries({ queryKey: ['admin', 'restore-status'] });
      setRestoringTimestamp(timestamp);
      setConfirmTs(null);
    },
    onError: (e: unknown) => {
      toast.error(e instanceof Error ? e.message : 'Could not start the restore.');
      setConfirmTs(null);
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
  const confirmPoint = confirmTs ? (points.find((p) => p.timestamp === confirmTs) ?? null) : null;
  const restoreData = restoreStatus.data;
  const showRestorePanel = restoringTimestamp !== null || restoreData?.state === 'failed';

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
                onRestore={(ts) => setConfirmTs(ts)}
                restoringTimestamp={restoringTimestamp}
              />
            ))
          )}
        </div>
      )}

      {showRestorePanel && (
        <div
          data-testid="restore-progress"
          role="status"
          aria-live="polite"
          className="rounded-md border p-4 space-y-3"
        >
          {restoringTimestamp !== null && restoreStatus.isError ? (
            <div data-testid="restore-degraded" className="space-y-1">
              <div className="text-sm font-medium text-amber-700 dark:text-amber-400">
                Restoring…
              </div>
              <p className="text-sm text-muted-foreground">
                The app is briefly unavailable while the database is restored — this can take a few
                minutes. This page keeps checking automatically.
              </p>
            </div>
          ) : restoreData?.state === 'failed' ? (
            <div data-testid="restore-failed" className="space-y-2">
              <div className="text-sm font-medium text-destructive">Restore failed</div>
              <p className="text-sm text-muted-foreground">
                {restoreData.error ?? 'The restore did not finish.'}
              </p>
              {restoreData.safety_backup_ts && (
                <p className="text-xs text-muted-foreground">
                  A safety backup was taken before this restore ({restoreData.safety_backup_ts}). You
                  can restore from it if needed.
                </p>
              )}
              <button
                type="button"
                className="self-start rounded-md border px-3 py-1.5 text-sm"
                onClick={() => queryClient.removeQueries({ queryKey: ['admin', 'restore-status'] })}
              >
                Dismiss
              </button>
            </div>
          ) : restoreData ? (
            <div className="space-y-2">
              <div className="text-sm font-medium">Restoring…</div>
              {restoreData.current_step && (
                <p className="text-sm text-muted-foreground">{restoreData.current_step}</p>
              )}
              <ol className="space-y-1">
                {restoreData.steps.map((step) => (
                  <li key={step.name} className="flex items-center gap-2 text-sm">
                    <span aria-hidden className="w-4 text-center">
                      {step.status === 'done'
                        ? '✓'
                        : step.status === 'failed'
                          ? '✗'
                          : step.status === 'running'
                            ? '…'
                            : '•'}
                    </span>
                    <span className={step.status === 'done' ? 'text-muted-foreground' : undefined}>
                      {step.name}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          ) : (
            <div className="text-sm text-muted-foreground">Starting the restore…</div>
          )}
        </div>
      )}

      <TypedConfirmDialog
        requiredWord="RESTORE"
        open={confirmTs !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmTs(null);
        }}
        title="Restore from this backup?"
        confirmLabel="Restore"
        description={
          <span>
            This replaces the current databases, search index, and provider keys with the contents
            of this backup
            {confirmPoint ? ` from ${new Date(confirmPoint.created_at).toLocaleString()}` : ''}. A
            safety backup is taken first, and the app is briefly unavailable while it restores. Type{' '}
            <span className="font-mono font-semibold">RESTORE</span> to confirm.
          </span>
        }
        onConfirm={() => {
          if (confirmTs) restoreMutation.mutate(confirmTs);
        }}
      />

      <RestoreRunbook />
    </div>
  );
}

/**
 * Admin Backup panel.
 *
 * Accessible at /admin/backups (admin role; AdminOnlyRoute guards the route).
 * Shows sidecar status, restore points grouped from GET /api/admin/backups/restore-points,
 * allows an on-demand backup (confirm), per-file download (expandable per card),
 * a guided one-click restore (typed-RESTORE confirm + polled progress that degrades
 * gracefully while the app is briefly unreachable mid-restore), an in-browser
 * off-host upload that stages another server's backup in the restore inbox, and
 * the manual host runbook as the advanced fallback.
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
  getInboxRestorePoints,
  triggerBackup,
  downloadBackup,
  requestRestore,
  getRestoreStatus,
  deleteRestorePoint,
  getRetention,
  putRetention,
  type RestorePoint,
  type InboxRestorePoint,
  type RestoreSource,
  type RetentionConfig,
} from '@/lib/api/backups';
import { useMaintenanceStore } from '@/stores/maintenance-store';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';
import { OffHostUploadSection } from '@/components/admin/OffHostUploadSection';
import { RestoreRunbook } from '@/components/admin/RestoreRunbook';
import { GuidedRecoveryView } from '@/components/admin/GuidedRecoveryView';
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

/** Parse a backup's %Y%m%d_%H%M%S key into a Date (local time); null if malformed. */
function parseBackupTs(ts: string): Date | null {
  const m = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(ts);
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  return new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s));
}

function InboxBadge({ ok, okLabel, badLabel }: { ok: boolean; okLabel: string; badLabel: string }) {
  return ok ? (
    <span className="inline-flex rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-400">
      {okLabel}
    </span>
  ) : (
    <span className="inline-flex rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">
      {badLabel}
    </span>
  );
}

/**
 * Off-host recovery: list the backup sets the operator has staged in this server's
 * restore inbox and trigger a cross-host restore. The trigger is disabled (with an
 * inline hint) until a set is complete, carries its secrets archive, AND its one-time
 * key is present, and while any restore is already in flight. A secrets-less set would
 * fail post-swap, so it is blocked here. Files are staged by the in-browser uploader
 * (OffHostUploadSection) or a host-side copy; the app only lists what the sidecar
 * reports and requests the restore.
 */
function InboxRestoreSection({
  points,
  restoringTimestamp,
  onRestore,
}: {
  points: InboxRestorePoint[];
  restoringTimestamp: string | null;
  onRestore: (timestamp: string) => void;
}) {
  return (
    <div className="rounded-md border p-4 space-y-3" data-testid="inbox-restore-section">
      <div>
        <h2 className="text-sm font-medium">Restore from another JARVIS</h2>
        <p className="text-xs text-muted-foreground">
          Recover this server from a backup taken on a different JARVIS. Upload that backup&apos;s
          archive set and its one-time key with the uploader above (or copy them into the
          server&apos;s restore inbox); staged sets appear below.
        </p>
      </div>
      {points.length === 0 ? (
        <div
          data-testid="inbox-empty"
          className="rounded-md border px-4 py-6 text-center text-sm text-muted-foreground"
        >
          No off-host backups staged. Upload a backup archive set and its one-time key to
          recover from another JARVIS.
        </div>
      ) : (
        <ul className="space-y-2">
          {points.map((p) => {
            const disabled =
              !p.complete || !p.has_secrets || !p.has_key || restoringTimestamp !== null;
            const hint = !p.complete
              ? 'This backup is missing a required database archive.'
              : !p.has_secrets
                ? 'This backup has no secrets archive; the restore would fail after swapping the databases. Stage the secrets archive before restoring.'
                : !p.has_key
                  ? 'Drop the one-time operator key into the restore inbox before restoring.'
                  : null;
            return (
              <li
                key={p.timestamp}
                data-testid="inbox-restore-point"
                className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3"
              >
                <div className="space-y-1">
                  <div className="text-sm font-medium">
                    {parseBackupTs(p.timestamp)?.toLocaleString() ?? p.timestamp}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    <InboxBadge ok={p.complete} okLabel="Complete" badLabel="Incomplete" />
                    <InboxBadge ok={p.has_secrets} okLabel="Secrets" badLabel="No secrets" />
                    <InboxBadge ok={p.has_key} okLabel="Key ready" badLabel="Key missing" />
                  </div>
                  {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
                </div>
                <button
                  type="button"
                  className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
                  disabled={disabled}
                  onClick={() => onRestore(p.timestamp)}
                >
                  Restore to this point
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function RestorePointCard({
  point,
  retentionDays,
  onDownload,
  onRestore,
  onDelete,
  restoringTimestamp,
}: {
  point: RestorePoint;
  retentionDays: number | null;
  onDownload: (name: string) => void;
  onRestore: (timestamp: string) => void;
  onDelete: (timestamp: string) => void;
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
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
            disabled={restoreDisabled}
            onClick={() => onRestore(point.timestamp)}
          >
            {isThisRestoring ? 'Restoring…' : 'Restore to this point'}
          </button>
          <button
            type="button"
            className="rounded-md border border-destructive px-3 py-1.5 text-sm font-medium text-destructive disabled:opacity-50"
            disabled={isThisRestoring}
            onClick={() => onDelete(point.timestamp)}
          >
            Delete
          </button>
        </div>
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
  const maintenanceActive = useMaintenanceStore((s) => s.active);
  const [confirming, setConfirming] = useState(false);
  const [confirmTs, setConfirmTs] = useState<string | null>(null);
  const [confirmSource, setConfirmSource] = useState<RestoreSource>('local');
  const [deleteConfirmTs, setDeleteConfirmTs] = useState<string | null>(null);
  const [restoringTimestamp, setRestoringTimestamp] = useState<string | null>(null);
  // One-time bearer token from POST /restore: keeps the progress poll authorized
  // DB-free after the restore tears down the admin session (see getRestoreStatus).
  const [restoreToken, setRestoreToken] = useState<string | null>(null);
  const [manualStepsNotice, setManualStepsNotice] = useState<string | null>(null);
  const [keepLastN, setKeepLastN] = useState('');
  const [maxAgeDays, setMaxAgeDays] = useState('');

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

  const inbox = useQuery({
    queryKey: ['admin', 'backups', 'inbox'],
    queryFn: getInboxRestorePoints,
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

  // Poll restore progress while a restore is being tracked — either one we started
  // this session, or (after the 503 interceptor flips maintenance) as long as we still
  // hold the one-time token. The token authorizes the poll DB-free so it survives the
  // DB swap that drops the admin session. A numeric refetchInterval keeps polling even
  // when the query errors, so the brief app-down window mid-restore degrades (below)
  // instead of dropping out.
  const trackingRestore = restoringTimestamp !== null || (maintenanceActive && restoreToken !== null);
  const restoreStatus = useQuery({
    queryKey: ['admin', 'restore-status'],
    queryFn: () => getRestoreStatus(restoreToken ?? undefined),
    enabled: trackingRestore,
    refetchInterval: trackingRestore ? 3000 : false,
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
      // Off-host / older-schema restores finish 'done' but stay held in
      // maintenance (every route still 503s) until the operator recreates the
      // app containers and clears the markers. Don't claim success there — show
      // a "one more step" notice pointing at the guided steps below instead.
      if (restoreStatus.data?.manual_steps_required === true) {
        setManualStepsNotice(
          restoreStatus.data.error ??
            'The restore finished but the app is held in maintenance until you recreate the app containers and clear the maintenance markers — see the steps below.',
        );
      } else {
        toast.success('Restore complete. Your data has been restored.');
      }
      void queryClient.invalidateQueries({ queryKey: ['admin', 'restore-points'] });
      setRestoringTimestamp(null);
      setRestoreToken(null);
    } else if (restoreState === 'failed') {
      setRestoringTimestamp(null);
      setRestoreToken(null);
    }
  }, [restoreState, restoringTimestamp, restoreStatus.data, queryClient]);

  const restoreMutation = useMutation({
    mutationFn: ({ timestamp, source }: { timestamp: string; source: RestoreSource }) =>
      requestRestore(timestamp, 'RESTORE', source),
    onSuccess: (data, { timestamp }) => {
      // Evict any terminal state (e.g. 'done') left by a previous restore so the
      // new restore starts from a clean fetch rather than the stale cached state.
      queryClient.removeQueries({ queryKey: ['admin', 'restore-status'] });
      // Capture the one-time bearer token so the poll survives the session teardown.
      setRestoreToken(data.status_token ?? null);
      setRestoringTimestamp(timestamp);
      setConfirmTs(null);
    },
    onError: (e: unknown) => {
      toast.error(e instanceof Error ? e.message : 'Could not start the restore.');
      setConfirmTs(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (timestamp: string) => deleteRestorePoint(timestamp, 'DELETE'),
    onSuccess: () => {
      toast.success('Delete requested. The restore point will be removed shortly.');
      setDeleteConfirmTs(null);
      void queryClient.invalidateQueries({ queryKey: ['admin', 'restore-points'] });
    },
    onError: (e: unknown) => {
      toast.error(e instanceof Error ? e.message : 'Could not delete the restore point.');
      setDeleteConfirmTs(null);
    },
  });

  const retentionQuery = useQuery({
    queryKey: ['admin', 'backups', 'retention'],
    queryFn: getRetention,
  });

  // Latches true once the policy has hydrated from the server, so Save can never
  // fire from the uninitialized '' defaults. Stays true afterward — a later
  // background refetch failure (isError) doesn't blank the already-loaded fields,
  // so it shouldn't re-lock Save either.
  const [retentionLoaded, setRetentionLoaded] = useState(false);

  // Seed the inputs from the saved policy once it loads (empty string == "no cap").
  useEffect(() => {
    if (retentionQuery.data) {
      setKeepLastN(retentionQuery.data.keep_last_n?.toString() ?? '');
      setMaxAgeDays(retentionQuery.data.max_age_days?.toString() ?? '');
      setRetentionLoaded(true);
    }
  }, [retentionQuery.data]);

  const retentionMutation = useMutation({
    mutationFn: putRetention,
    onSuccess: (data) => {
      toast.success('Retention policy saved.');
      queryClient.setQueryData(['admin', 'backups', 'retention'], data);
    },
    onError: (e: unknown) => {
      toast.error(e instanceof Error ? e.message : 'Could not save the retention policy.');
    },
  });

  const handleSaveRetention = () => {
    // Belt-and-suspenders: the Save button is already disabled until the policy
    // has loaded, but never build a PUT from uninitialized fields even so.
    if (!retentionLoaded) return;
    // Blank, 0, or an invalid value all mean "no cap" (null): a 0 window would be
    // a footgun (delete everything but the last day), and 0 kept points is
    // meaningless — both collapse to the default, matching the sidecar's floors.
    const parse = (raw: string): number | null => {
      const trimmed = raw.trim();
      if (trimmed === '') return null;
      const n = Number(trimmed);
      return Number.isFinite(n) && n >= 1 ? Math.floor(n) : null;
    };
    const config: RetentionConfig = {
      keep_last_n: parse(keepLastN),
      max_age_days: parse(maxAgeDays),
    };
    retentionMutation.mutate(config);
  };

  const handleDownload = async (name: string) => {
    try {
      await downloadBackup(name);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Download failed.');
    }
  };

  const points = restore?.restore_points ?? [];
  const inboxPoints = inbox.data ?? [];
  const confirmPoint = confirmTs ? (points.find((p) => p.timestamp === confirmTs) ?? null) : null;
  const deleteConfirmPoint = deleteConfirmTs
    ? (points.find((p) => p.timestamp === deleteConfirmTs) ?? null)
    : null;
  const restoreData = restoreStatus.data;
  // Open the shared typed-RESTORE confirm, remembering which source it targets.
  const askRestore = (timestamp: string, source: RestoreSource) => {
    setConfirmSource(source);
    setConfirmTs(timestamp);
  };
  const showRestorePanel =
    restoringTimestamp !== null ||
    restoreData?.state === 'failed' ||
    manualStepsNotice !== null ||
    (maintenanceActive && restoreToken !== null);

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
            `Last backup ${formatAge(status.last_run_at)} · ${points.length} restore point${points.length !== 1 ? 's' : ''}`
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

      {status?.trigger_pending && (
        <div
          data-testid="backup-running"
          role="status"
          aria-live="polite"
          className="rounded-md border border-primary/40 bg-primary/5 px-4 py-2 text-sm text-primary"
        >
          Backup running… This can take a few minutes; new restore points appear when it finishes.
        </div>
      )}

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
                onRestore={(ts) => askRestore(ts, 'local')}
                onDelete={(ts) => setDeleteConfirmTs(ts)}
                restoringTimestamp={restoringTimestamp}
              />
            ))
          )}
        </div>
      )}

      <OffHostUploadSection
        onUploaded={() =>
          void queryClient.invalidateQueries({ queryKey: ['admin', 'backups', 'inbox'] })
        }
      />

      {!inbox.isLoading && !inbox.isError && (
        <InboxRestoreSection
          points={inboxPoints}
          restoringTimestamp={restoringTimestamp}
          onRestore={(ts) => askRestore(ts, 'inbox')}
        />
      )}

      <div className="rounded-md border p-4 space-y-3" data-testid="retention-controls">
        <div>
          <h2 className="text-sm font-medium">Retention policy</h2>
          <p className="text-xs text-muted-foreground">
            How long backups are kept. Leave a field blank to use the default. Older or excess
            restore points are removed automatically by the backup service.
          </p>
        </div>
        {retentionQuery.isError && !retentionLoaded ? (
          <div className="text-sm text-destructive">
            Could not load the retention policy.
            <button
              type="button"
              className="ml-2 rounded-md border px-3 py-1.5 text-sm"
              onClick={() => void retentionQuery.refetch()}
            >
              Retry
            </button>
          </div>
        ) : (
          <div className="flex flex-wrap items-end gap-4">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-xs font-medium">Keep most recent</span>
              <input
                type="number"
                min={0}
                inputMode="numeric"
                aria-label="Keep most recent restore points"
                className="w-32 rounded-md border px-2 py-1 text-sm"
                placeholder="All"
                value={keepLastN}
                onChange={(e) => setKeepLastN(e.target.value)}
              />
              <span className="text-xs text-muted-foreground">restore points</span>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-xs font-medium">Maximum age</span>
              <input
                type="number"
                min={0}
                inputMode="numeric"
                aria-label="Maximum age in days"
                className="w-32 rounded-md border px-2 py-1 text-sm"
                placeholder="Default"
                value={maxAgeDays}
                onChange={(e) => setMaxAgeDays(e.target.value)}
              />
              <span className="text-xs text-muted-foreground">days</span>
            </label>
            <button
              type="button"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
              onClick={handleSaveRetention}
              disabled={retentionMutation.isPending || !retentionLoaded}
            >
              Save retention policy
            </button>
          </div>
        )}
      </div>

      {showRestorePanel && (
        <GuidedRecoveryView
          restoringTimestamp={restoringTimestamp}
          pollError={restoreStatus.isError}
          status={restoreData}
          manualStepsNotice={manualStepsNotice}
          onDismissFailed={() =>
            queryClient.removeQueries({ queryKey: ['admin', 'restore-status'] })
          }
          onDismissManual={() => setManualStepsNotice(null)}
        />
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
          if (confirmTs) restoreMutation.mutate({ timestamp: confirmTs, source: confirmSource });
        }}
      />

      <TypedConfirmDialog
        requiredWord="DELETE"
        open={deleteConfirmTs !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteConfirmTs(null);
        }}
        title="Delete this restore point?"
        confirmLabel="Delete"
        description={
          <span>
            This permanently deletes every archive in this restore point
            {deleteConfirmPoint
              ? ` from ${new Date(deleteConfirmPoint.created_at).toLocaleString()}`
              : ''}
            . This cannot be undone. Type{' '}
            <span className="font-mono font-semibold">DELETE</span> to confirm.
          </span>
        }
        onConfirm={() => {
          if (deleteConfirmTs) deleteMutation.mutate(deleteConfirmTs);
        }}
      />

      <RestoreRunbook />
    </div>
  );
}

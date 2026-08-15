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
  getInboxRestorePoints,
  triggerBackup,
  downloadBackup,
  deleteRestorePoint,
  type RestorePoint,
  type InboxRestorePoint,
  type RestoreSource,
} from '@/lib/api/backups';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useRestoreRecoveryController } from '@/hooks/use-restore-recovery-controller';
import { useRetentionForm } from '@/hooks/use-retention-form';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';
import { OffHostUploadSection } from '@/components/admin/OffHostUploadSection';
import { RestoreRunbook } from '@/components/admin/RestoreRunbook';
import { GuidedRecoveryView } from '@/components/admin/GuidedRecoveryView';
import { TypedConfirmDialog } from '@/components/admin/TypedConfirmDialog';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import { formatRelativeTime } from '@/lib/relative-time';
import { formatDateTime } from '@/lib/utils';

const STORE_LABELS: Record<string, string> = {
  jarvis: 'Main database',
  litellm: 'AI model router database',
  pdfs: 'PDF files',
  secrets: 'Data keys',
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
 * inline hint) until a set is complete, carries its data-key archive, AND its one-time
 * key is present, and while any restore is already in flight. Current backups also
 * require PDFs; an eligible older backup without PDFs gets a separate warning.
 * A set without data keys would fail post-swap, so it is blocked here. Files are staged by
 * the in-browser uploader (OffHostUploadSection) or a host-side copy; the app only
 * lists what the sidecar reports and requests the restore.
 */
function InboxRestoreSection({
  points,
  restoringTimestamp,
  onRestore,
}: {
  points: InboxRestorePoint[];
  restoringTimestamp: string | null;
  onRestore: (timestamp: string, allowMissingPdfs: boolean) => void;
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
            const legacyMissingPdfs = !p.has_pdfs && p.legacy_missing_pdfs;
            const missingCurrentPdfs = !p.has_pdfs && !p.legacy_missing_pdfs;
            const disabled =
              !p.complete ||
              !p.has_secrets ||
              !p.has_key ||
              missingCurrentPdfs ||
              restoringTimestamp !== null;
            let hint: string | null = null;
            if (missingCurrentPdfs) {
              hint = 'This backup is missing its required PDF archive and cannot be restored.';
            } else if (!p.complete) {
              hint = 'This backup is missing a required database archive.';
            } else if (!p.has_secrets) {
              hint =
                'This backup has no data-key archive. Add it before restoring; JARVIS will not change data without it.';
            } else if (!p.has_key) {
              hint = 'Drop the one-time operator key into the restore inbox before restoring.';
            } else if (legacyMissingPdfs) {
              hint =
                "This older backup has no PDF files. Restoring it will clear this server's current PDF files.";
            }
            return (
              <li
                key={p.timestamp}
                data-testid="inbox-restore-point"
                className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3"
              >
                <div className="space-y-1">
                  <div className="text-sm font-medium">
                    {formatDateTime(parseBackupTs(p.timestamp), p.timestamp)}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    <InboxBadge ok={p.complete} okLabel="Complete" badLabel="Incomplete" />
                    <InboxBadge
                      ok={p.has_pdfs}
                      okLabel="PDFs included"
                      badLabel={legacyMissingPdfs ? 'Older backup: no PDFs' : 'PDFs missing'}
                    />
                    <InboxBadge
                      ok={p.has_secrets}
                      okLabel="Data keys"
                      badLabel="No data keys"
                    />
                    <InboxBadge ok={p.has_key} okLabel="Key ready" badLabel="Key missing" />
                  </div>
                  {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
                </div>
                <button
                  type="button"
                  className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
                  disabled={disabled}
                  onClick={() => onRestore(p.timestamp, legacyMissingPdfs)}
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
  onRestore: (
    timestamp: string,
    allowMissingPdfs: boolean,
    schemaUncheckable: boolean,
  ) => void;
  onDelete: (timestamp: string) => void;
  restoringTimestamp: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const isNewer = point.compat === 'newer';
  const isThisRestoring = restoringTimestamp === point.timestamp;
  const legacyMissingPdfs = !point.has_pdfs && point.legacy_missing_pdfs;
  // Backups taken while the database was unreachable recorded no usable schema
  // version, so the restore service cannot check compatibility and refuses
  // without the operator's explicit acknowledgement in the confirm dialog.
  const unknownSchema = point.schema_version == null || point.schema_version === 0;
  const missingCurrentPdfs = !point.has_pdfs && !point.legacy_missing_pdfs;
  const restoreDisabled =
    isNewer || !point.complete || missingCurrentPdfs || restoringTimestamp !== null;
  const storeBadges = [
    ...point.stores.map(storeLabel),
    ...point.qdrant_collections.map((c) => `Qdrant: ${c}`),
  ];

  return (
    <div className="rounded-md border p-4 space-y-3" data-testid="restore-point-card">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="text-sm font-medium">{formatRelativeTime(point.created_at)}</div>
          <div className="text-xs text-muted-foreground">
            {formatDateTime(point.created_at)} · {formatBytes(point.total_size_bytes)}
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
            onClick={() => onRestore(point.timestamp, legacyMissingPdfs, unknownSchema)}
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
        {missingCurrentPdfs && (
          <span className="text-xs text-muted-foreground">
            This backup is missing its PDF archive and cannot be restored.
          </span>
        )}
        {legacyMissingPdfs && (
          <span className="text-xs text-muted-foreground">
            Older backup: PDF files were not included. Restoring it will clear this server&apos;s
            current PDF files.
          </span>
        )}
        {unknownSchema && (
          <span className="text-xs text-muted-foreground">
            This restore point predates schema recording, so JARVIS cannot check whether it fits
            this version.
          </span>
        )}
      </div>
    </div>
  );
}

export function AdminBackupsPage() {
  const queryClient = useQueryClient();
  const restoreController = useRestoreRecoveryController();
  const retention = useRetentionForm();
  const [confirming, setConfirming] = useState(false);
  const [confirmTs, setConfirmTs] = useState<string | null>(null);
  const [confirmSource, setConfirmSource] = useState<RestoreSource>('local');
  const [confirmAllowMissingPdfs, setConfirmAllowMissingPdfs] = useState(false);
  // Whether this restore point's database version can be checked before the
  // restore runs, and whether the operator has accepted going ahead without that
  // check. The acceptance is always the operator's own act — never derived.
  const [confirmSchemaUncheckable, setConfirmSchemaUncheckable] = useState(false);
  const [confirmSchemaAccepted, setConfirmSchemaAccepted] = useState(false);
  const [deleteConfirmTs, setDeleteConfirmTs] = useState<string | null>(null);

  const {
    data: status,
    isLoading: statusLoading,
    isError: statusError,
  } = useQuery({
    queryKey: QUERY_KEYS.admin.backupStatus(),
    queryFn: getBackupStatus,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });

  const {
    data: restore,
    isLoading,
    isError,
  } = useQuery({
    queryKey: QUERY_KEYS.admin.restorePoints(),
    queryFn: getRestorePoints,
    placeholderData: keepPreviousData,
  });

  const inbox = useQuery({
    queryKey: QUERY_KEYS.admin.backupInbox(),
    queryFn: getInboxRestorePoints,
    placeholderData: keepPreviousData,
  });

  const trigger = useMutation({
    mutationFn: triggerBackup,
    onSuccess: () => {
      toast.success('Backup requested. The backup runs in the background.');
      setConfirming(false);
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.restorePoints() });
    },
    onError: (e: unknown) => {
      toast.error(e instanceof Error ? e.message : 'Could not request a backup.');
      setConfirming(false);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (timestamp: string) => deleteRestorePoint(timestamp, 'DELETE'),
    onSuccess: () => {
      toast.success('Delete requested. The restore point will be removed shortly.');
      setDeleteConfirmTs(null);
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.restorePoints() });
    },
    onError: (e: unknown) => {
      toast.error(e instanceof Error ? e.message : 'Could not delete the restore point.');
      setDeleteConfirmTs(null);
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
  // A run succeeds when a complete restorable local set exists, which stays true
  // when the vector store could not be snapshotted or the off-site copy failed.
  // Those shortfalls have no other surface, so the status line has to name them.
  const noVectors = status?.last_run_vectors_captured === false;
  const noOffSite = status?.last_run_s3_complete === false;
  const incompleteCapture = noVectors
    ? noOffSite
      ? 'vectors were not captured and the off-site copy is incomplete'
      : 'vectors were not captured'
    : noOffSite
      ? 'the off-site copy is incomplete'
      : null;
  const inboxPoints = inbox.data ?? [];
  const confirmPoint = confirmTs ? (points.find((p) => p.timestamp === confirmTs) ?? null) : null;
  const deleteConfirmPoint = deleteConfirmTs
    ? (points.find((p) => p.timestamp === deleteConfirmTs) ?? null)
    : null;
  // Open the shared typed-RESTORE confirm, remembering which source it targets.
  const askRestore = (
    timestamp: string,
    source: RestoreSource,
    allowMissingPdfs: boolean,
    schemaUncheckable: boolean,
  ) => {
    setConfirmSource(source);
    setConfirmAllowMissingPdfs(allowMissingPdfs);
    setConfirmSchemaUncheckable(schemaUncheckable);
    setConfirmSchemaAccepted(false);
    setConfirmTs(timestamp);
  };
  return (
    <div className="p-6 space-y-6">
      <div>
        <AdminBreadcrumb page="Backups" />
        <h1 className="text-2xl font-semibold">Backups</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Backups include databases, PDF files, the search index, and the data keys used to read
          saved settings. A restore keeps this host&apos;s infrastructure credentials, while an
          off-host restore holds restored outbound connections for review. Downloads can contain
          private data and credentials, so keep them secure.
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
              {status.last_attempt_at ? ` (${formatRelativeTime(status.last_attempt_at)})` : ''}
            </span>
          ) : status?.last_run_succeeded && incompleteCapture ? (
            <span className="text-amber-700 dark:text-amber-400">
              {`Last backup completed, but ${incompleteCapture}.`}
              {status.last_run_at ? ` (${formatRelativeTime(status.last_run_at)})` : ''}
            </span>
          ) : status?.last_run_at ? (
            `Last backup ${formatRelativeTime(status.last_run_at)} · ${points.length} restore point${points.length !== 1 ? 's' : ''}`
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
                onRestore={(ts, allowMissingPdfs, schemaUncheckable) =>
                  askRestore(ts, 'local', allowMissingPdfs, schemaUncheckable)
                }
                onDelete={(ts) => setDeleteConfirmTs(ts)}
                restoringTimestamp={restoreController.restoringTimestamp}
              />
            ))
          )}
        </div>
      )}

      <OffHostUploadSection
        onUploaded={() =>
          void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.backupInbox() })
        }
      />

      {/* An off-host listing carries no database version, so the check is always
          unavailable here and the confirm dialog always asks the operator. */}
      {inbox.isError && <QueryErrorState message="Failed to load off-host backups." />}
      {!inbox.isLoading && !inbox.isError && (
        <InboxRestoreSection
          points={inboxPoints}
          restoringTimestamp={restoreController.restoringTimestamp}
          onRestore={(ts, allowMissingPdfs) => askRestore(ts, 'inbox', allowMissingPdfs, true)}
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
        {retention.isError && !retention.loaded ? (
          <div className="text-sm text-destructive">
            Could not load the retention policy.
            <button
              type="button"
              className="ml-2 rounded-md border px-3 py-1.5 text-sm"
              onClick={() => void retention.refetch()}
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
                value={retention.keepLastN}
                onChange={(e) => retention.setKeepLastN(e.target.value)}
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
                value={retention.maxAgeDays}
                onChange={(e) => retention.setMaxAgeDays(e.target.value)}
              />
              <span className="text-xs text-muted-foreground">days</span>
            </label>
            <button
              type="button"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
              onClick={retention.save}
              disabled={retention.isPending || !retention.loaded}
            >
              Save retention policy
            </button>
          </div>
        )}
      </div>

      {restoreController.showRestorePanel && (
        <GuidedRecoveryView
          restoringTimestamp={restoreController.restoringTimestamp}
          pollError={restoreController.pollError}
          status={restoreController.status}
          manualStepsNotice={restoreController.manualStepsNotice}
          quarantine={restoreController.quarantine}
          quarantineRestoreId={restoreController.quarantineRestoreId}
          recoveryIssue={restoreController.recoveryIssue}
          acknowledgementPending={restoreController.acknowledgementPending}
          onAcknowledge={
            restoreController.quarantine === 'awaiting_review' &&
            restoreController.quarantineRestoreId !== null
              ? () => restoreController.setAcknowledgementOpen(true)
              : null
          }
          onDismissFailed={restoreController.dismissFailed}
          onDismissManual={restoreController.dismissManual}
        />
      )}

      <TypedConfirmDialog
        requiredWord="I HAVE REVIEWED RESTORED CREDENTIALS"
        open={restoreController.acknowledgementOpen}
        onOpenChange={restoreController.setAcknowledgementOpen}
        title="Enable restored outbound connections?"
        confirmLabel="Acknowledge"
        description={
          <span>
            Confirm that you reviewed SMTP, Telegram, AI providers, Zotero, research sources, and
            scheduled deliveries for restore{' '}
            <span className="font-mono">{restoreController.quarantineRestoreId}</span>.
            This enables outbound use of restored database credentials. Type{' '}
            <span className="font-mono font-semibold">
              I HAVE REVIEWED RESTORED CREDENTIALS
            </span>{' '}
            to confirm.
          </span>
        }
        onConfirm={() => {
          restoreController.acknowledgeQuarantine();
        }}
      />

      <TypedConfirmDialog
        requiredWord="RESTORE"
        open={confirmTs !== null}
        onOpenChange={(open) => {
          if (!open) {
            setConfirmTs(null);
            setConfirmAllowMissingPdfs(false);
            setConfirmSchemaAccepted(false);
          }
        }}
        title={
          confirmAllowMissingPdfs
            ? 'Restore this older backup without PDFs?'
            : 'Restore from this backup?'
        }
        confirmLabel="Restore"
        description={
          <>
            {confirmSchemaUncheckable && (
              <span data-testid="restore-unknown-schema-warning">
                {confirmSource === 'inbox'
                  ? 'An off-host backup set carries no database version JARVIS can read up front, so it cannot check that this set fits this version before restoring.'
                  : 'This restore point predates schema recording, so JARVIS cannot check that it fits this version.'}{' '}
                Restore it anyway only if it is the restore point you need.{' '}
              </span>
            )}
            {confirmAllowMissingPdfs ? (
              <span>
                This older backup does not include PDF files. Restoring it will remove the PDF
                files currently stored on this server. Papers may still appear in JARVIS, but
                their PDF files will not open. A safety backup is taken first. Type{' '}
                <span className="font-mono font-semibold">RESTORE</span> to confirm.
              </span>
            ) : (
              <span>
                This replaces the current JARVIS data, saved database settings and credentials,
                data keys, search index, and PDF files with the contents of this backup
                {confirmPoint
                  ? ` from ${formatDateTime(confirmPoint.created_at)}`
                  : ''}
                . A safety backup is taken first. This host&apos;s infrastructure credentials
                stay unchanged; off-host outbound connections remain blocked until reviewed. The
                app is briefly unavailable while it restores. Type{' '}
                <span className="font-mono font-semibold">RESTORE</span> to confirm.
              </span>
            )}
            {confirmSchemaUncheckable && (
              <label className="mt-3 flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={confirmSchemaAccepted}
                  onChange={(e) => setConfirmSchemaAccepted(e.target.checked)}
                />
                <span>Go ahead without a version check.</span>
              </label>
            )}
          </>
        }
        onConfirm={() => {
          if (!confirmTs) return;
          if (confirmSchemaUncheckable && !confirmSchemaAccepted) {
            toast.error(
              'Tick "Go ahead without a version check" to restore a backup JARVIS cannot check.',
            );
            return;
          }
          restoreController.startRestore(
            {
              timestamp: confirmTs,
              source: confirmSource,
              allowMissingPdfs: confirmAllowMissingPdfs,
              allowUnknownSchema: confirmSchemaAccepted,
            },
            () => {
              setConfirmTs(null);
              setConfirmAllowMissingPdfs(false);
              setConfirmSchemaAccepted(false);
            },
          );
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
              ? ` from ${formatDateTime(deleteConfirmPoint.created_at)}`
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

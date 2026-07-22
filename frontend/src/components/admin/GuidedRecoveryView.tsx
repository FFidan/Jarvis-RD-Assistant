/**
 * Render the highest-priority recovery state and the current restore steps.
 * Quarantine and recovery errors take precedence over polling, failure, manual
 * action, and progress states. The live region announces state changes.
 */
import type { RestoreStatus } from '@/lib/api/backups';

const STEP_LABEL: Record<string, string> = {
  done: 'Done',
  failed: 'Failed',
  running: 'Active',
};

export function GuidedRecoveryView({
  restoringTimestamp,
  pollError,
  status,
  manualStepsNotice,
  quarantine,
  quarantineRestoreId,
  recoveryIssue,
  acknowledgementPending,
  onAcknowledge,
  onDismissFailed,
  onDismissManual,
}: {
  restoringTimestamp: string | null;
  pollError: boolean;
  status: RestoreStatus | undefined;
  manualStepsNotice: string | null;
  quarantine: RestoreStatus['quarantine'];
  quarantineRestoreId: string | null;
  recoveryIssue: string | null;
  acknowledgementPending: boolean;
  onAcknowledge: (() => void) | null;
  onDismissFailed: () => void;
  onDismissManual: () => void;
}) {
  const recovering = status?.phase === 'recover';
  return (
    <div
      data-testid="restore-progress"
      role="status"
      aria-live="polite"
      className="rounded-md border p-4 space-y-3"
    >
      {quarantine === 'unreadable' ? (
        <div data-testid="restore-quarantine" className="space-y-2">
          <div className="text-sm font-medium text-destructive">
            Restore review state needs host attention
          </div>
          <p className="text-sm text-muted-foreground">
            Outbound connections remain blocked because the restore review record cannot be read.
            Inspect the record on the host. After reviewing every connection, run{' '}
            <code>jarvis-research restore acknowledge &lt;restore-id&gt;</code> with the exact restore
            ID.
          </p>
        </div>
      ) : quarantine === 'awaiting_review' ? (
        <div data-testid="restore-quarantine" className="space-y-3">
          <div>
            <div className="text-sm font-medium text-amber-700 dark:text-amber-400">
              Review restored connections
            </div>
            <p className="text-sm text-muted-foreground">
              Local reads are available, but outbound use of restored database credentials stays
              blocked until this review is acknowledged.
            </p>
          </div>
          {quarantineRestoreId && (
            <p className="text-xs text-muted-foreground">
              Restore ID: <span className="font-mono">{quarantineRestoreId}</span>
            </p>
          )}
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
            <li>SMTP and sign-in email delivery</li>
            <li>Telegram bot access and notifications</li>
            <li>AI model providers and observability</li>
            <li>Zotero and scholarly-source credentials</li>
            <li>Pulse, digest, and other scheduled deliveries</li>
          </ul>
          <p className="text-xs text-muted-foreground">
            The destination host&apos;s files and infrastructure credentials were not replaced. Data
            keys and database-stored settings came from the backup.
          </p>
          {recoveryIssue && (
            <p className="text-sm text-amber-700 dark:text-amber-400">{recoveryIssue}</p>
          )}
          <p className="text-xs text-muted-foreground">
            If this tab&apos;s restore session is unavailable, sign in as the configured owner or run{' '}
            <code>jarvis-research restore acknowledge &lt;restore-id&gt;</code> on the host. Other
            administrators cannot acknowledge this review.
          </p>
          {onAcknowledge && (
            <button
              type="button"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
              onClick={onAcknowledge}
              disabled={acknowledgementPending}
            >
              Acknowledge review
            </button>
          )}
        </div>
      ) : restoringTimestamp !== null && pollError ? (
        <div data-testid="restore-degraded" className="space-y-1">
          <div className="text-sm font-medium text-amber-700 dark:text-amber-400">Restoring…</div>
          <p className="text-sm text-muted-foreground">
            The app is briefly unavailable while the database is restored — this can take a few
            minutes. This page keeps checking automatically.
          </p>
        </div>
      ) : status?.state === 'failed' ? (
        <div data-testid="restore-failed" className="space-y-2">
          <div className="text-sm font-medium text-destructive">Restore failed</div>
          <p className="text-sm text-muted-foreground">
            {status.error ?? 'The restore did not finish.'}
          </p>
          {status.safety_backup_ts && (
            <p className="text-xs text-muted-foreground">
              A safety backup was taken before this restore ({status.safety_backup_ts}). You can
              restore from it if needed.
            </p>
          )}
          <button
            type="button"
            className="self-start rounded-md border px-3 py-1.5 text-sm"
            onClick={onDismissFailed}
          >
            Dismiss
          </button>
        </div>
      ) : manualStepsNotice !== null ? (
        <div data-testid="restore-manual-steps" className="space-y-2">
          <div className="text-sm font-medium text-amber-700 dark:text-amber-400">
            One more step needed to finish
          </div>
          <p className="text-sm text-muted-foreground">{manualStepsNotice}</p>
          <p className="text-xs text-muted-foreground">
            Follow the guided steps below to finish and bring the app back online.
          </p>
          <button
            type="button"
            className="self-start rounded-md border px-3 py-1.5 text-sm"
            onClick={onDismissManual}
          >
            Dismiss
          </button>
        </div>
      ) : status ? (
        <div className="space-y-2">
          <div className="text-sm font-medium">
            {recovering ? 'Auto-recovering an interrupted restore…' : 'Restoring…'}
          </div>
          {status.current_step && (
            <p className="text-sm text-muted-foreground">{status.current_step}</p>
          )}
          <ol className="space-y-1">
            {status.steps.map((step) => (
              <li key={step.name} className="flex items-center gap-2 text-sm">
                <span aria-hidden className="w-12 text-xs text-muted-foreground">
                  {STEP_LABEL[step.status] ?? 'Pending'}
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
  );
}

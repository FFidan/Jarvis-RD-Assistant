/**
 * Guided recovery view — the live panel an admin watches through a restore.
 *
 * Driven entirely by the token-authenticated restore-status poll so it keeps
 * showing progress even while the restore drops/recreates the databases (the point
 * where the admin's own session dies) — the initiating admin never sees a blank 503.
 * It renders, in priority order: a degraded "still restoring" note when the poll is
 * briefly unreachable, a terminal failure with the exact next action, a "one more
 * step" notice for a restore held in maintenance, an auto-recovering note while a
 * crash-recovery reconciles, otherwise the live step list. The wrapper is an
 * `aria-live` region so step changes are announced to assistive tech.
 */
import type { RestoreStatus } from '@/lib/api/backups';

const STEP_GLYPH: Record<string, string> = {
  done: '✓',
  failed: '✗',
  running: '…',
};

export function GuidedRecoveryView({
  restoringTimestamp,
  pollError,
  status,
  manualStepsNotice,
  onDismissFailed,
  onDismissManual,
}: {
  restoringTimestamp: string | null;
  pollError: boolean;
  status: RestoreStatus | undefined;
  manualStepsNotice: string | null;
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
      {restoringTimestamp !== null && pollError ? (
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
                <span aria-hidden className="w-4 text-center">
                  {STEP_GLYPH[step.status] ?? '•'}
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

/**
 * SmtpMisconfigBanner — shared warning for the SMTP settings card and the
 * first-run wizard, shown whenever the backend reports SMTP configuration
 * issues. This includes the case where the relay looks deliverable (host +
 * sender present) but a username is set without a resolvable password, which
 * still 535s at send time — so the banner must surface issues even when
 * `deliverable` is true.
 *
 * Renders nothing only when there are no issues. Kept in one module so the
 * Settings card and the wizard step never drift in copy or styling. The issue
 * strings are produced (value-free) by the backend (`GET /api/setup/smtp` →
 * `issues`) so the wording lives server-side.
 */
interface SmtpMisconfigBannerProps {
  /** Effective-config deliverability from GET /api/setup/smtp. */
  deliverable?: boolean;
  /** Value-free, operator-facing issue strings. */
  issues?: string[];
  className?: string;
}

export function SmtpMisconfigBanner({ deliverable: _deliverable, issues, className }: SmtpMisconfigBannerProps) {
  // Warn whenever the backend reports explicit issues — including the
  // host+sender-set-but-no-password case, where `deliverable` stays true but
  // sends still fail. `deliverable` is accepted for API compatibility but no
  // longer gates the warning (the issue list is authoritative).
  if (!issues || issues.length === 0) return null;
  return (
    <div
      role="alert"
      data-testid="smtp-misconfig-banner"
      className={
        'rounded-md border border-amber-500 bg-amber-50 px-4 py-3 text-sm ' +
        'text-amber-900 dark:bg-amber-950/20 dark:text-amber-300' +
        (className ? ` ${className}` : '')
      }
    >
      <p className="font-medium">Sign-in emails aren’t being delivered yet</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-5">
        {issues.map((issue) => (
          <li key={issue}>{issue}</li>
        ))}
      </ul>
    </div>
  );
}

/**
 * SmtpSection — Email / SMTP relay settings (System, admin-only).
 *
 * On mount: GET /api/setup/smtp hydrates host/port/user/from_email/reply_to/
 * from_name and reports effective-config health (deliverable + issues) for the
 * misconfiguration banner. Password is never returned by the server;
 * `has_password` indicates whether one is stored. Leaving the password field
 * blank on Save keeps the existing password — it is only overwritten when the
 * user types a new value. Reply-To / display name are NOT secrets: blank
 * clears them.
 *
 * The misconfig banner renders ALONGSIDE the editable form (not as an early
 * return) so the admin can fix the problem in place. The early-return is
 * reserved for a genuine load failure (`isError`).
 */
import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getSmtpConfig, saveSmtpConfig } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { errorMessage } from '@/lib/errors';
import type { SmtpConfig } from '@/types';
import { SmtpMisconfigBanner } from './SmtpMisconfigBanner';

// ---------------------------------------------------------------------------
// SmtpSection
// ---------------------------------------------------------------------------

export function SmtpSection() {
  const qc = useQueryClient();

  const { data: config, isLoading, isError: isConfigError } = useQuery<SmtpConfig>({
    queryKey: QUERY_KEYS.account.smtp(),
    queryFn: getSmtpConfig,
    staleTime: 60_000,
  });

  // Form state — seeded from config once loaded
  const [host, setHost] = useState('');
  const [port, setPort] = useState('587');
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [fromEmail, setFromEmail] = useState('');
  const [replyTo, setReplyTo] = useState('');
  const [fromName, setFromName] = useState('');
  const [testRecipient, setTestRecipient] = useState('');
  const [hydrated, setHydrated] = useState(false);

  // Hydrate form fields once the query resolves (only on first load)
  useEffect(() => {
    if (config && !hydrated) {
      setHost(config.host ?? '');
      setPort(config.port != null ? String(config.port) : '587');
      setUser(config.user ?? '');
      setFromEmail(config.from_email ?? '');
      setReplyTo(config.reply_to ?? '');
      setFromName(config.from_name ?? '');
      setHydrated(true);
    }
  }, [config, hydrated]);

  const saveMut = useMutation({
    mutationFn: saveSmtpConfig,
    onSuccess: () => {
      // Refetch so the deliverability banner reflects the saved config.
      qc.invalidateQueries({ queryKey: QUERY_KEYS.account.smtp() });
    },
  });

  const portNum = parseInt(port, 10);
  const portError =
    port !== '' && (Number.isNaN(portNum) || portNum < 1 || portNum > 65535)
      ? 'Port must be a number between 1 and 65535'
      : null;
  // Reply-To is optional: blank is valid; otherwise require a valid email.
  const replyToError =
    replyTo !== '' && !/^\S+@\S+\.\S+$/.test(replyTo)
      ? 'Enter a valid email address or leave blank'
      : null;

  const canSave = !!host && !!fromEmail && portError === null && replyToError === null;

  const buildBody = (testSend: boolean) => ({
    host,
    port: portNum,
    user: user ?? '',
    // Send password only when the user typed something. Empty string →
    // backend keeps the existing password (setup.py `if body.password:`).
    password,
    from_email: fromEmail,
    // Reply-To / display name: always sent as the current field value, so a
    // blank field clears the stored value (backend treats "" as cleared).
    reply_to: replyTo,
    from_name: fromName,
    test_send: testSend,
    test_recipient: testRecipient || undefined,
  });

  const handleSave = (testSend: boolean) => {
    if (!canSave || Number.isNaN(portNum)) return;
    saveMut.mutate(buildBody(testSend));
  };

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading SMTP settings…</p>;
  }

  if (isConfigError) {
    return (
      <p className="text-sm text-destructive" role="alert">
        Failed to load SMTP settings. Please refresh.
      </p>
    );
  }

  const hasExistingPassword = config?.has_password ?? false;
  const restartRequired = config?.restart_required ?? false;
  const saveOk = saveMut.isSuccess && saveMut.data?.saved;
  const testSent = saveMut.data?.test_sent;
  const testError = saveMut.data?.test_error;

  return (
    <div className="space-y-6">
      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <p className="text-sm text-muted-foreground">
            Configure the outgoing mail server used to send sign-in links to users.
            Your password is stored encrypted and is never shown.
          </p>
        </CardHeader>

        <CardContent className="space-y-4">
          {/* Effective-config health — rendered with the form so the admin can fix it. */}
          <SmtpMisconfigBanner deliverable={config?.deliverable} issues={config?.issues} />

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="smtp-host">Host</Label>
              <Input
                id="smtp-host"
                value={host}
                onChange={(e) => setHost(e.target.value)}
                placeholder="smtp.resend.com"
                autoComplete="off"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="smtp-port">Port</Label>
              <Input
                id="smtp-port"
                value={port}
                onChange={(e) => setPort(e.target.value)}
                inputMode="numeric"
                autoComplete="off"
                aria-invalid={portError !== null}
                aria-describedby={portError ? 'smtp-port-error' : undefined}
              />
              {portError && (
                <p id="smtp-port-error" className="text-xs text-destructive">
                  {portError}
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="smtp-user">Username</Label>
              <Input
                id="smtp-user"
                value={user}
                onChange={(e) => setUser(e.target.value)}
                placeholder="resend"
                autoComplete="username"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="smtp-pass">
                Password
                {hasExistingPassword && !password && (
                  <span className="ml-2 text-xs text-muted-foreground font-normal">
                    currently set — leave blank to keep it
                  </span>
                )}
              </Label>
              <Input
                id="smtp-pass"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={hasExistingPassword ? '•••• stored' : ''}
                autoComplete="new-password"
              />
            </div>

            <div className="sm:col-span-2 space-y-1.5">
              <Label htmlFor="smtp-from">From address</Label>
              <Input
                id="smtp-from"
                type="email"
                value={fromEmail}
                onChange={(e) => setFromEmail(e.target.value)}
                placeholder="jarvis@your-domain.dev"
                autoComplete="off"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="smtp-reply-to">Reply-To address (optional)</Label>
              <Input
                id="smtp-reply-to"
                type="email"
                value={replyTo}
                onChange={(e) => setReplyTo(e.target.value)}
                placeholder="replies-go-here@your-domain.dev"
                autoComplete="off"
                aria-invalid={replyToError !== null}
                aria-describedby={replyToError ? 'smtp-reply-to-error' : undefined}
              />
              {replyToError && (
                <p id="smtp-reply-to-error" className="text-xs text-destructive">
                  {replyToError}
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="smtp-from-name">Sender display name (optional)</Label>
              <Input
                id="smtp-from-name"
                value={fromName}
                onChange={(e) => setFromName(e.target.value)}
                placeholder="JARVIS RD"
                autoComplete="off"
              />
            </div>

            <div className="sm:col-span-2 space-y-1.5">
              <Label htmlFor="smtp-test">Test recipient (optional)</Label>
              <Input
                id="smtp-test"
                type="email"
                value={testRecipient}
                onChange={(e) => setTestRecipient(e.target.value)}
                placeholder="defaults to the From address"
                autoComplete="off"
              />
            </div>
          </div>

          {/* Save / test buttons + feedback */}
          <div className="flex flex-wrap items-center gap-3 pt-2">
            <Button
              onClick={() => handleSave(false)}
              disabled={saveMut.isPending || !canSave}
            >
              {saveMut.isPending ? 'Saving…' : 'Save'}
            </Button>

            <Button
              variant="outline"
              onClick={() => handleSave(true)}
              disabled={saveMut.isPending || !canSave}
            >
              Save & send test email
            </Button>

            {saveOk && testSent !== true && testError == null && (
              <p className="text-sm text-green-600 dark:text-green-400">
                {restartRequired
                  ? 'Saved. An administrator must restart the app for the new SMTP settings to take effect.'
                  : 'SMTP settings saved and active immediately.'}
              </p>
            )}

            {saveOk && testSent === true && (
              <p className="text-sm text-green-600 dark:text-green-400">
                Saved — test email sent successfully.
              </p>
            )}

            {testSent === false && testError && (
              <p className="text-sm text-destructive" role="alert">
                Saved, but the test send failed: {testError}
              </p>
            )}

            {saveMut.isError && (
              <p className="text-sm text-destructive" role="alert">
                Could not save: {errorMessage(saveMut.error, 'unknown error')}
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

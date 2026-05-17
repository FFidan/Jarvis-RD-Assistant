/**
 * SmtpSection — Email / SMTP relay settings (§IV System, admin-only).
 *
 * On mount: GET /api/setup/smtp hydrates host/port/user/from_email.
 * Password is never returned by the server; `has_password` indicates
 * whether one is stored. Leaving the password field blank on Save will
 * keep the existing password — it is only overwritten when the user types
 * a new value.
 *
 * The `restart_required` flag is read from the GET /api/setup/smtp response
 * (backend now always returns false — changes apply immediately). The UI
 * shows an honest success note based on the flag value.
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getSmtpConfig, saveSmtpConfig } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import type { SmtpConfig } from '@/types';

// ---------------------------------------------------------------------------
// SmtpSection
// ---------------------------------------------------------------------------

export function SmtpSection() {
  const qc = useQueryClient();

  const { data: config, isLoading } = useQuery<SmtpConfig>({
    queryKey: ['smtp-config'],
    queryFn: getSmtpConfig,
    staleTime: 60_000,
  });

  // Form state — seeded from config once loaded
  const [host, setHost] = useState('');
  const [port, setPort] = useState('587');
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [fromEmail, setFromEmail] = useState('');
  const [hydrated, setHydrated] = useState(false);

  // Hydrate form fields once the query resolves (only on first load)
  if (config && !hydrated) {
    setHost(config.host ?? '');
    setPort(config.port != null ? String(config.port) : '587');
    setUser(config.user ?? '');
    setFromEmail(config.from_email ?? '');
    setHydrated(true);
  }

  const saveMut = useMutation({
    mutationFn: saveSmtpConfig,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['smtp-config'] });
    },
  });

  const handleSave = () => {
    const portNum = parseInt(port, 10);
    if (!host || !fromEmail || Number.isNaN(portNum)) return;
    saveMut.mutate({
      host,
      port: portNum,
      user: user ?? '',
      // Send password only when the user typed something. Empty string →
      // backend skips the update (line 440 of setup.py: `if body.password:`).
      password: password,
      from_email: fromEmail,
    });
  };

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading SMTP settings…</p>;
  }

  const hasExistingPassword = config?.has_password ?? false;
  // restart_required comes from the backend (now always false — SMTP settings
  // are applied immediately without requiring a service restart).
  const restartRequired = config?.restart_required ?? false;
  const saveOk = saveMut.isSuccess && saveMut.data?.saved;

  return (
    <div className="space-y-6">
      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <p className="text-sm text-muted-foreground">
            Configure the outgoing mail server used to send login links to users.
            Your password is stored encrypted and is never shown.
          </p>
        </CardHeader>

        <CardContent className="space-y-4">
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
              />
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
          </div>

          {/* Save button + feedback */}
          <div className="flex flex-wrap items-center gap-3 pt-2">
            <Button
              onClick={handleSave}
              disabled={saveMut.isPending || !host || !fromEmail}
            >
              {saveMut.isPending ? 'Saving…' : 'Save'}
            </Button>

            {saveOk && (
              <p className="text-sm text-green-600 dark:text-green-400">
                {restartRequired
                  ? 'Saved. An administrator must restart the app for the new SMTP settings to take effect.'
                  : 'SMTP settings saved and active immediately.'}
              </p>
            )}

            {saveMut.isError && (
              <p className="text-sm text-destructive">
                Could not save: {saveMut.error instanceof Error ? saveMut.error.message : 'unknown error'}
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

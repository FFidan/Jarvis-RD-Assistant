import { useState, useEffect, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Fingerprint, X } from 'lucide-react';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useAuthStore } from '@/stores/auth-store';
import { usePasskeys } from '@/hooks/usePasskeys';
import { LoginPasskeyButton } from '@/components/auth/LoginPasskeyButton';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { requestMagicLink, ApiError, getFirstRunStatus } from '@/lib/api';

const PASSKEY_PROMO_DISMISS_KEY = 'jarvis-passkey-promo-dismissed';

/**
 * Magic-link login surface — SMTP-aware.
 *
 * Default tab: determined by the pre-auth `/api/setup/status` payload.
 *
 * - setup_mode=single -> API-key tab is primary.
 * - setup_mode=multi -> magic-link tab is primary.
 * - smtp_configured=false, single mode -> API-key tab is primary; magic-link
 *   tab remains available but annotated to explain that email is not configured.
 * - smtp_configured=false, multi mode -> magic-link tab stays primary with an
 *   honest notice: links cannot be delivered until an admin configures SMTP.
 *   The API-key tab is still reachable but API-key login is rejected by the
 *   backend once more than one account exists (unless API_KEY_LOGIN_ENABLED=true).
 * - setup_mode absent and smtp_configured absent (fetch failed / older backend) ->
 *   magic-link tab stays primary (safe fallback, no behavior change for
 *   existing installs).
 *
 * Anti-enumeration (D4): `request-link` always returns `{sent: true}`.  The
 * UI must NOT infer delivery success from that response.  `smtp_configured`
 * on the pre-auth status endpoint is the correct signal — it is a global
 * server-side probe, never a per-request delivery receipt.
 */
export function LoginPage() {
  const { login } = useAuthStore();
  const [searchParams] = useSearchParams();
  const initialError = searchParams.get('error');

  const { data: firstRunStatus } = useQuery({
    queryKey: QUERY_KEYS.setup.firstRun(),
    queryFn: getFirstRunStatus,
    staleTime: 60_000,
    retry: false,
    retryOnMount: false,
  });

  const smtpConfigured = firstRunStatus?.smtp_configured;
  const smtpReachable = firstRunStatus?.smtp_reachable;
  const setupMode = firstRunStatus?.setup_mode;
  const isMultiMode = setupMode === 'multi';

  const [mode, setMode] = useState<'magic-link' | 'api-key'>('magic-link');
  const [userChoseSide, setUserChoseSide] = useState(false);

  useEffect(() => {
    if (userChoseSide) return;
    if (setupMode === 'single') {
      setMode('api-key');
      return;
    }
    if (setupMode === 'multi') {
      setMode('magic-link');
      return;
    }
    // In multi-user mode without SMTP, stay on magic-link so the honest notice
    // is visible. The API-key tab is reachable but the backend rejects it once
    // more than one account exists (unless API_KEY_LOGIN_ENABLED=true).
    // In single-user mode without SMTP, default to API-key (the working path).
    if (smtpConfigured === false && !isMultiMode) {
      setMode('api-key');
    }
  }, [smtpConfigured, isMultiMode, setupMode, userChoseSide]);

  const [email, setEmail] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState(initialError ?? '');
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);

  const { capable: passkeysCapable } = usePasskeys();
  const [passkeyPromoDismissed, setPasskeyPromoDismissed] = useState(() => {
    try {
      return localStorage.getItem(PASSKEY_PROMO_DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  });

  function dismissPasskeyPromo() {
    setPasskeyPromoDismissed(true);
    try {
      localStorage.setItem(PASSKEY_PROMO_DISMISS_KEY, '1');
    } catch {
      // Private-mode / storage-disabled: dismissal just won't persist.
    }
  }

  async function handleMagicLinkSubmit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim()) {
      setError('Email is required');
      return;
    }
    setLoading(true);
    setError('');
    setInfo('');
    try {
      await requestMagicLink(email.trim());
      setInfo(
        smtpConfigured === false
          ? 'If an account exists for that address, a sign-in link has been created. Email is not set up on this server, so ask your admin to share your sign-in link with you.'
          : 'If an account exists for that address, a sign-in link is on its way.',
      );
      setEmail('');
    } catch (err) {
      // A 422 means the submitted value failed server-side email validation
      // (request-link otherwise returns sent:true unconditionally to avoid user
      // enumeration). Surface an input-specific message; anything else is transport.
      if (err instanceof ApiError && err.status === 422) {
        setError('Please enter a valid email address.');
      } else {
        setError('Could not send link — check your connection and try again.');
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleApiKeySubmit(e: FormEvent) {
    e.preventDefault();
    if (!apiKey.trim()) {
      setError('API key is required');
      return;
    }
    setLoading(true);
    setError('');
    const success = await login(apiKey.trim());
    setLoading(false);
    if (!success) {
      // Surface the precise backend reason (e.g. the 403 multi-tenant-disabled
      // message) rather than a generic string so the user knows to use
      // magic-link instead of retrying a doomed API-key login.
      const backendError = useAuthStore.getState().lastError;
      setError(backendError ?? 'Invalid API key or backend unreachable');
      setApiKey('');
    }
  }

  const smtpNotConfiguredNotice =
    smtpConfigured === false ? (
      isMultiMode ? (
        <p className="text-sm text-amber-600" role="status">
          Email is not configured on this server, so magic links can&apos;t be
          delivered automatically. Ask your admin to create a sign-in link for you
          and share it directly — no email required. (An admin can also set up
          email later so links arrive on their own.)
        </p>
      ) : (
        <p className="text-sm text-amber-600" role="status">
          Email is not configured on this server. Magic links will not be delivered.
          Use the API key tab to sign in.
        </p>
      )
    ) : null;

  // Configured-but-failing: the relay is set up but a cached liveness probe
  // could not reach it, so links may silently fail to arrive. Mutually
  // exclusive with the not-configured notice above (that requires
  // smtp_configured === false), so only one renders at a time.
  const smtpUnreachableNotice =
    smtpConfigured === true && smtpReachable === false ? (
      <p className="text-sm text-amber-600" role="status">
        Email is configured but the mail server is not responding right now —
        sign-in links may not arrive. An administrator can check the mail settings.
      </p>
    ) : null;

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl">JARVIS RD Assistant</CardTitle>
          <CardDescription>
            {mode === 'magic-link'
              ? 'Enter your email to receive a sign-in link'
              : 'Enter your API key to continue'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {mode === 'magic-link' ? (
            <form onSubmit={handleMagicLinkSubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError('');
                    setInfo('');
                  }}
                  placeholder="you@example.com"
                  autoComplete="email"
                  autoFocus
                  disabled={loading}
                />
              </div>
              {smtpNotConfiguredNotice}
              {smtpUnreachableNotice}
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
              {info && <p className="text-sm text-emerald-600" aria-live="polite">{info}</p>}
              <p className="text-xs text-muted-foreground">
                Already requested a link in the last 2 minutes? Wait before retrying —
                only one link is sent per 2 minutes.
              </p>
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? 'Sending...' : 'Send magic link'}
              </Button>
              <button
                type="button"
                className="block w-full text-center text-xs text-muted-foreground hover:underline"
                onClick={() => {
                  setMode('api-key');
                  setUserChoseSide(true);
                  setError('');
                  setInfo('');
                }}
              >
                Use API key instead
              </button>
            </form>
          ) : (
            <form onSubmit={handleApiKeySubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="apiKey">API Key</Label>
                <Input
                  id="apiKey"
                  type="password"
                  value={apiKey}
                  onChange={(e) => {
                    setApiKey(e.target.value);
                    setError('');
                  }}
                  placeholder="Your API key"
                  autoComplete="off"
                  autoFocus
                  disabled={loading}
                />
              </div>
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? 'Verifying...' : 'Sign In'}
              </Button>
              <button
                type="button"
                className="block w-full text-center text-xs text-muted-foreground hover:underline"
                onClick={() => {
                  setMode('magic-link');
                  setUserChoseSide(true);
                  setError('');
                }}
              >
                Use magic link instead
              </button>
            </form>
          )}

          {/* Passkey sign-in — progressive enhancement. The button renders only
              where passkeys are usable; otherwise a short, mode-aware note. */}
          <div className="mt-6 space-y-3">
            {passkeysCapable && (
              <div className="relative" aria-hidden>
                <div className="absolute inset-0 flex items-center">
                  <span className="w-full border-t border-hair" />
                </div>
                <div className="relative flex justify-center">
                  <span className="bg-card px-2 text-xs text-muted-foreground">or</span>
                </div>
              </div>
            )}
            <LoginPasskeyButton />
          </div>

          {/* One-time nudge after requesting a magic link: offer the faster path
              for next time. Only where passkeys work; dismissal persists. */}
          {passkeysCapable && info && !passkeyPromoDismissed && (
            <div
              className="mt-4 flex items-start gap-2 rounded-md border border-hair bg-muted/40 p-3"
              role="note"
            >
              <Fingerprint className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground" aria-hidden />
              <div className="flex-1 text-xs">
                <p className="font-medium text-foreground">Make sign-in easier on this device</p>
                <p className="mt-1 text-muted-foreground">
                  Once you&apos;re signed in, add a passkey from Settings so next time you
                  can sign in with your fingerprint, face, or device PIN.
                </p>
              </div>
              <button
                type="button"
                aria-label="Dismiss passkey suggestion"
                className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
                onClick={dismissPasskeyPromo}
              >
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

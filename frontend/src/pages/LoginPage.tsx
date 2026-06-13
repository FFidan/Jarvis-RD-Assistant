import { useState, useEffect, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useAuthStore } from '@/stores/auth-store';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { requestMagicLink, ApiError } from '@/lib/api';
import type { FirstRunStatus } from '@/lib/api';

/**
 * Magic-link login surface — SMTP-aware.
 *
 * Default tab: determined by SMTP availability read from the already-fetched
 * pre-auth `/api/setup/status` (TanStack Query cache warmed by App.tsx before
 * LoginPage is rendered).  This is a **cache-only read** — no additional
 * network request is made.
 *
 * - smtp_configured=true  → magic-link tab is primary (existing behavior).
 * - smtp_configured=false, single mode → API-key tab is primary; magic-link
 *   tab remains available but annotated to explain that email is not configured.
 * - smtp_configured=false, multi mode → magic-link tab stays primary with an
 *   honest notice: links cannot be delivered until an admin configures SMTP.
 *   The API-key tab is still reachable but API-key login is rejected by the
 *   backend once more than one account exists (unless API_KEY_LOGIN_ENABLED=true).
 * - smtp_configured absent (cache miss / fetch failed / older backend) →
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

  // Cache-only read: a second useQuery subscription here would refetch an
  // errored first-run query and re-enter App's loading gate.
  const queryClient = useQueryClient();
  const cachedFirstRun = queryClient.getQueryData<FirstRunStatus>(
    QUERY_KEYS.setup.firstRun(),
  );

  const smtpConfigured = cachedFirstRun?.smtp_configured;
  const isMultiMode = cachedFirstRun?.setup_mode === 'multi';

  const [mode, setMode] = useState<'magic-link' | 'api-key'>('magic-link');
  const [userChoseSide, setUserChoseSide] = useState(false);

  useEffect(() => {
    // In multi-user mode without SMTP, stay on magic-link so the honest notice
    // is visible. The API-key tab is reachable but the backend rejects it once
    // more than one account exists (unless API_KEY_LOGIN_ENABLED=true).
    // In single-user mode without SMTP, default to API-key (the working path).
    if (!userChoseSide && smtpConfigured === false && !isMultiMode) {
      setMode('api-key');
    }
  }, [smtpConfigured, isMultiMode, userChoseSide]);

  const [email, setEmail] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState(initialError ?? '');
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);

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
      setInfo('Check your email — we just sent you a sign-in link.');
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
          Email is not configured on this server — magic links cannot be delivered.
          Ask your admin to configure SMTP so sign-in links can be sent.
          API-key sign-in works only while a single account exists or when the
          operator has enabled it explicitly.
        </p>
      ) : (
        <p className="text-sm text-amber-600" role="status">
          Email is not configured on this server. Magic links will not be delivered.
          Use the API key tab to sign in.
        </p>
      )
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
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
              {info && <p className="text-sm text-emerald-600" aria-live="polite">{info}</p>}
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
                  placeholder="Enter JARVIS_API_KEY"
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
        </CardContent>
      </Card>
    </main>
  );
}
